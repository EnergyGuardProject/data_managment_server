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

from fastapi import APIRouter, Depends
from minio.error import S3Error

from app.config import settings
from app.dependencies import verify_api_key
from app.models import ProvisionRequest, ProvisionResult
from app.services.minio_client import (
    download_dataset_to_cache,
    get_minio_client,
)

router = APIRouter(prefix="/provision", tags=["provision"])
logger = logging.getLogger(__name__)

_AuthDep = Annotated[str, Depends(verify_api_key)]

DATASET_DIR_MODE = 0o755
NOTEBOOK_DIR_MODE = 0o777
NOTEBOOK_FILE_MODE = 0o666


def _set_mode(path: Path, mode: int) -> None:
    """Best-effort permission normalization for bind-mounted shared paths."""
    os.chmod(path, mode)


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

        for dataset_owner, dataset_name in req.datasets.items():
            try:
                files = download_dataset_to_cache(
                    client, dataset_owner, dataset_name, req.username
                )
                if files:
                    datasets_provisioned.append(f"{dataset_owner}/{dataset_name}")
                else:
                    errors.append(
                        f"Dataset '{dataset_name}' (owner: {dataset_owner}) is empty in MinIO."
                    )
            except S3Error as exc:
                errors.append(
                    f"Dataset '{dataset_name}' (owner: {dataset_owner}): MinIO error – {exc}"
                )
            except Exception as exc:
                errors.append(f"Dataset '{dataset_name}' (owner: {dataset_owner}): {exc}")

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
                response = client.get_object(settings.notebooks_bucket, obj.object_name)
                try:
                    dest_file.write_bytes(response.read())
                finally:
                    response.close()
                    response.release_conn()
                _set_mode(dest_file, NOTEBOOK_FILE_MODE)
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
