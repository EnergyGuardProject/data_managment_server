import logging
import os
from pathlib import Path

from minio import Minio
from minio.error import S3Error

from app.config import settings

logger = logging.getLogger(__name__)

DATASET_DIR_MODE = 0o755
DATASET_FILE_MODE = 0o644


def _set_mode(path: Path, mode: int) -> None:
    """Best-effort permission normalization for bind-mounted shared paths."""
    os.chmod(path, mode)


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
    *,
    overwrite: bool = True,
) -> list[str]:
    """Download every file under user_{owner}/{dataset_name}/ into
    {jupyterhub_data_path}/datasets/{target_username}/{dataset_name}/.

    When *overwrite* is ``False``, files that already exist locally are kept
    as-is and only missing files are downloaded.

    Returns the list of filenames that were written.
    """
    prefix = f"user_{owner}/{dataset_name}/"
    dest_dir = (
        Path(settings.jupyterhub_data_path)
        / "datasets"
        / target_username
        / dataset_name
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    _set_mode(dest_dir, DATASET_DIR_MODE)

    objects = list(
        client.list_objects(settings.datasets_bucket, prefix=prefix, recursive=True)
    )
    minio_paths = {
        Path(obj.object_name.removeprefix(prefix))
        for obj in objects
        if obj.object_name and obj.object_name.startswith(prefix)
    }

    if overwrite:
        for local_path in sorted(dest_dir.rglob("*"), reverse=True):
            if local_path.is_file() and local_path.relative_to(dest_dir) not in minio_paths:
                local_path.unlink()
            elif local_path.is_dir() and not any(local_path.iterdir()):
                local_path.rmdir()

    if not objects:
        if not any(dest_dir.iterdir()):
            dest_dir.rmdir()
        return []

    downloaded: list[str] = []
    for obj in objects:
        relative_name = obj.object_name.removeprefix(prefix)
        if not relative_name:
            continue
        dest_file = dest_dir / relative_name
        if not overwrite and dest_file.exists():
            logger.debug("File already exists, skipping: %s", dest_file)
            downloaded.append(relative_name)
            continue
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        _set_mode(dest_file.parent, DATASET_DIR_MODE)
        response = client.get_object(settings.datasets_bucket, obj.object_name)
        try:
            dest_file.write_bytes(response.read())
        finally:
            response.close()
            response.release_conn()
        _set_mode(dest_file, DATASET_FILE_MODE)
        downloaded.append(relative_name)

    if not downloaded and not any(dest_dir.iterdir()):
        dest_dir.rmdir()

    logger.info(
        "Cached dataset user_%s/%s for user %s (%d files)",
        owner,
        dataset_name,
        target_username,
        len(downloaded),
    )
    return downloaded

# def find_dataset_owner(
#     client: Minio,
#     dataset_name: str,
#     username: str,
# ) -> str | None:
#     """Return the MinIO owner prefix that contains dataset_name.  Returns None if not found anywhere.
#     """
#     candidates = [username]
#     candidates.append(settings.pilot_prefix.removeprefix("user_"))

#     for candidate in candidates:
#         prefix = f"user_{candidate}/{dataset_name}/"
#         objs = list(
#             client.list_objects(
#                 settings.datasets_bucket, prefix=prefix, recursive=False
#             )
#         )
#         if objs:
#             return candidate
#     return None
