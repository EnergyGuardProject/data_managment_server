import io
import json
import logging
import shutil
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from minio.error import S3Error

from app.config import settings
from app.dependencies import verify_api_key
from app.models import DatasetInfo, DatasetUpdateRequest, UpdateResult
from app.services.minio_client import download_dataset_to_cache, get_minio_client

router = APIRouter(prefix="/datasets", tags=["datasets"])
logger = logging.getLogger(__name__)

_AuthDep = Annotated[str, Depends(verify_api_key)]


@router.post("/upload", summary="Upload a dataset file (and optional metadata) to MinIO")
async def upload_dataset(
    _key: _AuthDep,
    username: str = Form(...),
    dataset_name: str = Form(...),
    file: UploadFile = File(..., description="CSV / tabular data file"),
    metadata: Optional[str] = Form(
        None, description="Optional JSON string with dataset metadata"
    ),
):
    """Store the dataset under ``user_{username}/{dataset_name}/data.csv`` in
    the ``datasets`` bucket.  If *metadata* is provided it is stored alongside
    as ``metadata.json``.
    """
    client = get_minio_client()
    object_prefix = f"user_{username}/{dataset_name}"
    data_object = f"{object_prefix}/data.csv"

    try:
        content = await file.read()
        client.put_object(
            settings.datasets_bucket,
            data_object,
            io.BytesIO(content),
            length=len(content),
            content_type=file.content_type or "text/csv",
        )
        logger.info("Uploaded dataset %s/%s", username, dataset_name)

        if metadata:
            try:
                meta_dict = json.loads(metadata)
            except json.JSONDecodeError:
                meta_dict = {"raw": metadata}
            meta_bytes = json.dumps(meta_dict, indent=2).encode()
            client.put_object(
                settings.datasets_bucket,
                f"{object_prefix}/metadata.json",
                io.BytesIO(meta_bytes),
                length=len(meta_bytes),
                content_type="application/json",
            )
    except S3Error as exc:
        logger.error("MinIO upload error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Storage error: {exc}")

    return {"status": "ok", "object": data_object}


@router.post("/metadata", summary="Upload or replace a dataset's metadata.json")
async def upload_metadata(
    _key: _AuthDep,
    username: str = Form(...),
    dataset_name: str = Form(...),
    metadata: UploadFile = File(...),
):
    client = get_minio_client()
    object_path = f"user_{username}/{dataset_name}/metadata.json"
    try:
        content = await metadata.read()
        client.put_object(
            settings.datasets_bucket,
            object_path,
            io.BytesIO(content),
            length=len(content),
            content_type="application/json",
        )
    except S3Error as exc:
        raise HTTPException(status_code=500, detail=f"Storage error: {exc}")

    return {"status": "ok", "object": object_path}


@router.get("", response_model=list[DatasetInfo], summary="List datasets in MinIO")
def list_datasets(_key: _AuthDep, username: Optional[str] = None):
    """List all datasets.  Pass ``username`` to filter to a single user."""
    client = get_minio_client()
    try:
        prefix = f"user_{username}/" if username else ""
        objects = list(
            client.list_objects(
                settings.datasets_bucket, prefix=prefix, recursive=True
            )
        )
    except S3Error as exc:
        raise HTTPException(status_code=500, detail=f"Storage error: {exc}")

    # Group object keys by (owner_prefix, dataset_name)
    grouped: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for obj in objects:
        parts = obj.object_name.split("/")
        if len(parts) < 3:
            continue
        owner, dataset = parts[0], parts[1]
        grouped.setdefault((owner, dataset), []).append(
            (obj.object_name, obj.size or 0)
        )

    return [
        DatasetInfo(
            owner=owner,
            name=dataset,
            files=[f for f, _ in files],
            size_bytes=sum(s for _, s in files),
        )
        for (owner, dataset), files in grouped.items()
    ]


@router.delete("/{username}/{dataset_name}", summary="Delete a dataset from MinIO and local cache")
def delete_dataset(_key: _AuthDep, username: str, dataset_name: str):
    client = get_minio_client()
    prefix = f"user_{username}/{dataset_name}/"
    try:
        objects = list(
            client.list_objects(settings.datasets_bucket, prefix=prefix, recursive=True)
        )
        if not objects:
            raise HTTPException(status_code=404, detail="Dataset not found in MinIO")
        for obj in objects:
            client.remove_object(settings.datasets_bucket, obj.object_name)
    except S3Error as exc:
        raise HTTPException(status_code=500, detail=f"Storage error: {exc}")

    # Remove from local JupyterHub cache as well (best-effort)
    local_path = (
        Path(settings.jupyterhub_data_path) / "datasets" / username / dataset_name
    )
    if local_path.exists():
        shutil.rmtree(local_path)

    return {"status": "deleted", "dataset": f"user_{username}/{dataset_name}"}


@router.post(
    "/update",
    response_model=UpdateResult,
    summary="Re-download a dataset into every user cache that currently holds it",
)
def update_dataset_for_all_users(_key: _AuthDep, req: DatasetUpdateRequest):
    """Called by the dashboard (or Dagster) when a dataset changes.  Walks the
    local cache and refreshes the dataset for every user whose cache directory
    contains it.
    """
    client = get_minio_client()
    base_datasets = Path(settings.jupyterhub_data_path) / "datasets"
    users_updated: list[str] = []
    errors: list[str] = []

    if not base_datasets.exists():
        return UpdateResult(
            dataset_owner=req.dataset_owner,
            dataset_name=req.dataset_name,
            users_updated=[],
            errors=[],
        )

    for user_dir in base_datasets.iterdir():
        if not user_dir.is_dir():
            continue
        if not (user_dir / req.dataset_name).exists():
            continue
        try:
            download_dataset_to_cache(
                client, req.dataset_owner, req.dataset_name, user_dir.name
            )
            users_updated.append(user_dir.name)
        except Exception as exc:
            errors.append(f"{user_dir.name}: {exc}")

    return UpdateResult(
        dataset_owner=req.dataset_owner,
        dataset_name=req.dataset_name,
        users_updated=users_updated,
        errors=errors,
    )
