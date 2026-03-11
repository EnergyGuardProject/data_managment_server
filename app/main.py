import logging

from fastapi import FastAPI
from minio.error import S3Error

from app.config import settings
from app.routers import datasets, notebooks, provision
from app.services.minio_client import ensure_buckets, get_minio_client

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="EnergyGuard Data Management Server",
    version="1.0.0",
    description=(
        "Internal service that manages dataset and notebook storage in MinIO "
        "and provisions files into JupyterHub user directories."
    ),
)

app.include_router(datasets.router, prefix="/api/v1")
app.include_router(notebooks.router, prefix="/api/v1")
app.include_router(provision.router, prefix="/api/v1")


@app.on_event("startup")
def on_startup() -> None:
    logger.info("Data Management Server starting up …")
    try:
        client = get_minio_client()
        ensure_buckets(client)
        logger.info("MinIO buckets verified/created.")
    except S3Error as exc:
        logger.error("MinIO startup check failed: %s", exc)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}
