import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from minio.error import S3Error

from app.config import settings
from app.dependencies import verify_api_key
from app.models import NotebookInfo
from app.services.minio_client import get_minio_client

router = APIRouter(prefix="/notebooks", tags=["notebooks"])
logger = logging.getLogger(__name__)

_AuthDep = Annotated[str, Depends(verify_api_key)]


@router.get("", response_model=list[NotebookInfo], summary="List notebooks available in MinIO")
def list_notebooks(_key: _AuthDep):
    """Return every .ipynb object stored in the ``notebooks`` bucket."""
    client = get_minio_client()
    try:
        objects = list(
            client.list_objects(settings.notebooks_bucket, recursive=True)
        )
    except S3Error as exc:
        raise HTTPException(status_code=500, detail=f"Storage error: {exc}")

    return [
        NotebookInfo(name=obj.object_name, size_bytes=obj.size or 0)
        for obj in objects
        if obj.object_name.endswith(".ipynb")
    ]
