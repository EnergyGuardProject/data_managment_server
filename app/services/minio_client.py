import logging
from pathlib import Path

from minio import Minio
from minio.error import S3Error

from app.config import settings

logger = logging.getLogger(__name__)


def get_minio_client() -> Minio:
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def ensure_buckets(client: Minio) -> None:
    """Create the datasets and notebooks buckets if they do not already exist."""
    for bucket in (settings.datasets_bucket, settings.notebooks_bucket):
        try:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
                logger.info("Created MinIO bucket: %s", bucket)
            else:
                logger.debug("MinIO bucket already exists: %s", bucket)
        except S3Error as exc:
            logger.error("Failed to ensure bucket %s: %s", bucket, exc)
            raise


def download_dataset_to_cache(
    client: Minio,
    owner: str,
    dataset_name: str,
    target_username: str,
) -> list[str]:
    """Download every file under user_{owner}/{dataset_name}/ into
    {jupyterhub_data_path}/datasets/{target_username}/{dataset_name}/.

    Returns the list of filenames that were written.
    """
    prefix = f"user_{owner}/{dataset_name}/"
    objects = list(
        client.list_objects(settings.datasets_bucket, prefix=prefix, recursive=True)
    )
    if not objects:
        return []

    dest_dir = (
        Path(settings.jupyterhub_data_path)
        / "datasets"
        / target_username
        / dataset_name
    )
    dest_dir.mkdir(parents=True, exist_ok=True)

    downloaded: list[str] = []
    for obj in objects:
        filename = obj.object_name.split("/")[-1]
        if not filename:
            continue
        dest_file = dest_dir / filename
        response = client.get_object(settings.datasets_bucket, obj.object_name)
        try:
            dest_file.write_bytes(response.read())
        finally:
            response.close()
            response.release_conn()
        downloaded.append(filename)

    logger.info(
        "Cached dataset user_%s/%s for user %s (%d files)",
        owner,
        dataset_name,
        target_username,
        len(downloaded),
    )
    return downloaded

def find_dataset_owner(
    client: Minio,
    dataset_name: str,
    username: str,
) -> str | None:
    """Return the MinIO owner prefix that contains dataset_name.  Returns None if not found anywhere.
    """
    candidates = [username]
    candidates.append(settings.pilot_prefix.removeprefix("user_"))

    for candidate in candidates:
        prefix = f"user_{candidate}/{dataset_name}/"
        objs = list(
            client.list_objects(
                settings.datasets_bucket, prefix=prefix, recursive=False
            )
        )
        if objs:
            return candidate
    return None
