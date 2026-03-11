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


@router.post("/upload", summary="Upload dataset files (and optional metadata) to MinIO")
async def upload_dataset(
    _key: _AuthDep,
    username: str = Form(...),
    dataset_name: str = Form(...),
    files: list[UploadFile] = File(..., description="Files that belong to the dataset"),
    metadata: Optional[UploadFile] = File(
        None, description="Optional JSON metadata file"
    ),
):
    """Store dataset files under ``user_{username}/{dataset_name}/dataset_<filename>``
    in the ``datasets`` bucket. If *metadata* is provided it is stored
    alongside as ``metadata_<filename>``.
    """
    client = get_minio_client()
    object_prefix = f"user_{username}/{dataset_name}"
    uploaded_objects: list[str] = []

    try:
        for file in files:
            dataset_filename = Path(file.filename or "data").name
            data_object = f"{object_prefix}/{dataset_filename}"
            content = await file.read()
            client.put_object(
                settings.datasets_bucket,
                data_object,
                io.BytesIO(content),
                length=len(content),
                content_type=file.content_type or "application/octet-stream",
            )
            uploaded_objects.append(data_object)
        logger.info("Uploaded dataset %s/%s", username, dataset_name)

        if metadata:
            try:
                meta_content = await metadata.read()
                meta_dict = json.loads(meta_content)
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=400, detail=f"Invalid metadata JSON: {exc}")
            metadata_filename = Path(metadata.filename or "metadata.json").name
            meta_bytes = json.dumps(meta_dict, indent=2).encode()
            client.put_object(
                settings.datasets_bucket,
                f"{object_prefix}/{metadata_filename}",
                io.BytesIO(meta_bytes),
                length=len(meta_bytes),
                content_type="application/json",
            )
            uploaded_objects.append(f"{object_prefix}/metadata_{metadata_filename}")
    except S3Error as exc:
        logger.error("MinIO upload error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Storage error: {exc}")

    return {"status": "ok", "objects": uploaded_objects}


@router.post("/metadata", summary="Upload or replace a dataset's metadata.json")
async def upload_metadata(
    _key: _AuthDep,
    username: str = Form(...),
    dataset_name: str = Form(...),
    metadata: UploadFile = File(...),
):
    client = get_minio_client()
    try:
        content = await metadata.read()
        meta_dict = json.loads(content)
        metadata_filename = Path(metadata.filename or "metadata.json").name
        object_path = f"user_{username}/{dataset_name}/{metadata_filename}"
        payload = json.dumps(meta_dict, indent=2).encode()
        client.put_object(
            settings.datasets_bucket,
            object_path,
            io.BytesIO(payload),
            length=len(payload),
            content_type="application/json",
        )
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid metadata JSON: {exc}")
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
        grouped.setdefault((owner, dataset), []).append(("/".join(parts[2:]), obj.size or 0))

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
        for obj in objects:
            client.remove_object(settings.datasets_bucket, obj.object_name)
    except S3Error as exc:
        raise HTTPException(status_code=500, detail=f"Storage error: {exc}")

    # Remove from every user's local JupyterHub cache as well (best-effort).
    base_datasets = Path(settings.jupyterhub_data_path) / "datasets"
    if base_datasets.exists():
        for user_dir in base_datasets.iterdir():
            if not user_dir.is_dir():
                continue
            local_path = user_dir / dataset_name
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
