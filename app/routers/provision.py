"""Provision endpoint: called by the dashboard backend just before it
redirects a user to their JupyterHub session.

The server:
1. Downloads each requested dataset from its explicit owner
2. Downloads missing / all requested datasets to the shared host cache.
3. Downloads notebooks that the user does not already have (unless
   force_notebook_refresh is True).

The shared cache at ``/jupyterhub_data`` is bind-mounted read-only into each
JupyterHub singleuser container under ``/home/jovyan/datasets`` and
``/home/jovyan/notebooks`` (configured in jupyterhub_config.py).
"""

import logging
import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from minio.error import S3Error

from app.config import settings
from app.dependencies import verify_api_key
from app.models import PilotProvisionRequest, ProvisionRequest, ProvisionResult
from app.pilots import PARTNERS, normalize_partner, pilot_file_name
from app.services.minio_client import (
    download_dataset_to_cache,
    download_object_atomic,
    get_minio_client,
)

router = APIRouter(prefix="/provision", tags=["provision"])
logger = logging.getLogger(__name__)

_AuthDep = Annotated[str, Depends(verify_api_key)]

DATASET_DIR_MODE = 0o755
NOTEBOOK_DIR_MODE = 0o777
NOTEBOOK_FILE_MODE = 0o666


def _set_mode(path: Path, mode: int) -> None:
    """Best-effort permission normalization for bind-mounted shared paths.

    Shared dirs may already exist and be owned by another service (e.g.
    JupyterHub's pre_spawn_hook runs as root and creates the same per-user
    dirs). A chmod EPERM in that case is expected and non-fatal — the hook
    already set the intended mode.
    """
    try:
        os.chmod(path, mode)
    except PermissionError as exc:
        logger.warning("chmod %o on %s skipped: %s", mode, path, exc)


def _safe_component(value: str, field: str) -> str:
    """Validate a caller-supplied string used as a single path component.

    ``username`` and ``dataset_name`` both come from the dashboard and both get
    joined onto a filesystem path, so a value containing ``/`` or ``..`` would
    let a caller write outside the shared directory.
    """
    candidate = (value or "").strip()
    if not candidate or candidate in {".", ".."} or "/" in candidate or "\0" in candidate:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field}: must be a single non-empty path component.",
        )
    return candidate


@router.post("/user", response_model=ProvisionResult, summary="Provision datasets and notebooks for a JupyterHub user")
def provision_user(_key: _AuthDep, req: ProvisionRequest):
    client = get_minio_client()
    datasets_provisioned: list[str] = []
    notebooks_provisioned: list[str] = []
    errors: list[str] = []

    # ── Datasets ──────────────────────────────────────────────────────────────
    if req.datasets:
        datasets_base = (
            Path(settings.jupyterhub_data_path) / "datasets" / req.username
        )
        datasets_base.mkdir(parents=True, exist_ok=True)
        _set_mode(datasets_base, DATASET_DIR_MODE)

        for dataset_minio_path, dataset_name in req.datasets.items():
            # ``dataset_minio_path`` is the bucket-relative MinIO prefix
            # (``user_<owner>/<original_name>``); ``dataset_name`` is the
            # name the user picked and the folder name to materialize on disk.
            path = dataset_minio_path.strip("/")
            if path.startswith("user_"):
                path = path[len("user_"):]
            try:
                dataset_owner, minio_dataset_name = path.split("/", 1)
            except ValueError:
                errors.append(
                    f"Invalid dataset_minio_path '{dataset_minio_path}': "
                    "expected 'user_<owner>/<dataset_name>'."
                )
                continue
            try:
                files = download_dataset_to_cache(
                    client, dataset_owner, minio_dataset_name, req.username,
                    overwrite=False, local_dataset_name=dataset_name,
                )
                if files:
                    datasets_provisioned.append(
                        f"{dataset_minio_path} -> {dataset_name}"
                    )
                else:
                    errors.append(
                        f"Dataset '{dataset_minio_path}' is empty in MinIO."
                    )
            except S3Error as exc:
                errors.append(
                    f"Dataset '{dataset_minio_path}': MinIO error – {exc}"
                )
            except Exception as exc:
                errors.append(f"Dataset '{dataset_minio_path}': {exc}")

    # ── Notebooks ─────────────────────────────────────────────────────────────
    # req.notebooks == None  → provision all available notebooks
    # req.notebooks == []    → skip notebook provisioning
    if req.notebooks is None or req.notebooks:
        notebooks_base = (
            Path(settings.jupyterhub_data_path) / "notebooks" / req.username
        )
        notebooks_base.mkdir(parents=True, exist_ok=True)
        _set_mode(notebooks_base, NOTEBOOK_DIR_MODE)

        try:
            all_objects = list(
                client.list_objects(settings.notebooks_bucket, recursive=True)
            )
            available = [
                obj for obj in all_objects if obj.object_name.endswith(".ipynb")
            ]
        except S3Error as exc:
            errors.append(f"Could not list notebooks: {exc}")
            available = []

        # Filter to requested names if an explicit list was given
        if req.notebooks:
            requested_set = set(req.notebooks)
            available = [o for o in available if o.object_name in requested_set]

        for obj in available:
            dest_file = notebooks_base / obj.object_name
            if dest_file.exists() and not req.force_notebook_refresh:
                logger.debug("Notebook already exists, skipping: %s", obj.object_name)
                continue
            try:
                download_object_atomic(
                    client,
                    settings.notebooks_bucket,
                    obj.object_name,
                    dest_file,
                    mode=NOTEBOOK_FILE_MODE,
                )
                notebooks_provisioned.append(obj.object_name)
                logger.info("Provisioned notebook %s for %s", obj.object_name, req.username)
            except S3Error as exc:
                errors.append(f"Notebook '{obj.object_name}': {exc}")
            except Exception as exc:
                errors.append(f"Notebook '{obj.object_name}': {exc}")

    return ProvisionResult(
        username=req.username,
        datasets_provisioned=datasets_provisioned,
        notebooks_provisioned=notebooks_provisioned,
        errors=errors,
    )


@router.post(
    "/pilot",
    response_model=ProvisionResult,
    summary="Link a pilot dataset into a JupyterHub user's datasets directory",
)
def provision_pilot(_key: _AuthDep, req: PilotProvisionRequest):
    """Give a user access to a pilot dataset — by symlink, never by copy.

    Pilot data is platform-owned and byte-identical for every user, so exactly
    one copy exists on disk: the nightly export in
    ``<shared>/pilot_datasets/<PARTNER>/``. That directory is bind-mounted
    read-only into every singleuser container at ``settings.pilot_mount_path``.
    All this endpoint does is create

        <shared>/datasets/<username>/<dataset_name>
            -> <pilot_mount_path>/<PARTNER>

    Two consequences worth spelling out:

    * The symlink target is a *container-side* absolute path, so the link is
      dangling when viewed on the host and resolves correctly inside the user's
      server. That is intended.
    * We create it under the user's already-mounted data directory, so it shows
      up in a server that is **already running** — Jupyter caches no filesystem
      state, and no restart or respawn is needed.

    Copying instead would multiply CEDER's ~500 MB (several GB uncompressed) by
    the number of users, which is exactly what the per-user dataset path does
    and what this endpoint exists to avoid.
    """
    partner = normalize_partner(req.partner)
    if partner is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown pilot partner '{req.partner}'. "
                f"Known partners: {', '.join(PARTNERS)}."
            ),
        )

    username = _safe_component(req.username, "username")
    dataset_name = _safe_component(req.dataset_name, "dataset_name")

    errors: list[str] = []
    datasets_provisioned: list[str] = []

    datasets_base = Path(settings.jupyterhub_data_path) / "datasets" / username
    datasets_base.mkdir(parents=True, exist_ok=True)
    _set_mode(datasets_base, DATASET_DIR_MODE)

    # Container-side target. Built with posix separators on purpose: it is
    # interpreted inside the singleuser container, not on this filesystem.
    target = f"{settings.pilot_mount_path.rstrip('/')}/{partner}"
    link_path = datasets_base / dataset_name

    # The export may not have run yet for this partner. Still link it — the
    # nightly job will fill it in — but tell the caller.
    source_file = (
        Path(settings.jupyterhub_data_path)
        / settings.pilot_datasets_prefix
        / partner
        / pilot_file_name(partner)
    )
    if not source_file.exists():
        errors.append(
            f"Pilot data for '{partner}' has not been exported yet "
            f"({source_file} is missing); the link will resolve once it has."
        )

    try:
        if link_path.is_symlink():
            # Idempotent: an identical link is a no-op, a stale one is repointed.
            if os.readlink(link_path) == target:
                logger.info(
                    "Pilot dataset '%s' (%s) already linked for %s",
                    dataset_name, partner, username,
                )
            else:
                link_path.unlink()
                os.symlink(target, link_path)
                logger.info(
                    "Re-pointed pilot dataset '%s' to %s for %s",
                    dataset_name, target, username,
                )
            datasets_provisioned.append(f"{partner} -> {dataset_name}")
        elif link_path.exists():
            errors.append(
                f"'{dataset_name}' already exists in {username}'s datasets "
                "directory as a real file or directory; refusing to replace it."
            )
        else:
            os.symlink(target, link_path)
            datasets_provisioned.append(f"{partner} -> {dataset_name}")
            logger.info(
                "Linked pilot dataset '%s' (%s) -> %s for %s",
                dataset_name, partner, target, username,
            )
    except FileExistsError:
        # Concurrent identical call won the race — that is still success.
        datasets_provisioned.append(f"{partner} -> {dataset_name}")
    except OSError as exc:
        errors.append(f"Could not link pilot dataset '{partner}': {exc}")

    return ProvisionResult(
        username=req.username,
        datasets_provisioned=datasets_provisioned,
        notebooks_provisioned=[],
        errors=errors,
    )


# @router.post(
#     "/sync-pilot-datasets",
#     response_model=list[dict],
#     summary="(Dagster) Re-download all pilot datasets for every user that has them cached",
# )
# def sync_pilot_datasets(_key: _AuthDep):
#     """Meant to be called periodically by Dagster.

#     Iterates the local cache and refreshes every pilot dataset found in any
#     user's directory.
#     """
#     client = get_minio_client()
#     base_datasets = Path(settings.jupyterhub_data_path) / "datasets"
#     results: list[dict] = []

#     if not base_datasets.exists():
#         return results

#     # Discover all pilot datasets currently in MinIO
#     pilot_prefix_full = f"user_{settings.pilot_prefix.removeprefix('user_')}/"
#     try:
#         pilot_objects = list(
#             client.list_objects(
#                 settings.datasets_bucket, prefix=pilot_prefix_full, recursive=False
#             )
#         )
#     except S3Error as exc:
#         raise HTTPException(status_code=500, detail=f"MinIO error: {exc}")

#     pilot_datasets = {
#         obj.object_name.rstrip("/").split("/")[-1]
#         for obj in pilot_objects
#         if obj.is_dir
#     }
#     if not pilot_datasets:
#         # Fall back: list one level and extract dataset names from object paths
#         try:
#             pilot_objects_r = list(
#                 client.list_objects(
#                     settings.datasets_bucket, prefix=pilot_prefix_full, recursive=True
#                 )
#             )
#             pilot_datasets = {
#                 obj.object_name.split("/")[1]
#                 for obj in pilot_objects_r
#                 if len(obj.object_name.split("/")) >= 3
#             }
#         except S3Error:
#             pilot_datasets = set()

#     pilot_owner = settings.pilot_prefix.removeprefix("user_")

#     for user_dir in base_datasets.iterdir():
#         if not user_dir.is_dir():
#             continue
#         username = user_dir.name
#         for dataset_name in pilot_datasets:
#             if not (user_dir / dataset_name).exists():
#                 continue
#             users_updated: list[str] = []
#             errs: list[str] = []
#             try:
#                 download_dataset_to_cache(client, pilot_owner, dataset_name, username)
#                 users_updated.append(username)
#             except Exception as exc:
#                 errs.append(f"{username}: {exc}")
#             results.append(
#                 {
#                     "dataset": dataset_name,
#                     "users_updated": users_updated,
#                     "errors": errs,
#                 }
#             )

#     return results
