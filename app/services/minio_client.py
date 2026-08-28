import logging
import os
import shutil
from pathlib import Path

from minio import Minio
from minio.error import S3Error

from app.config import settings

logger = logging.getLogger(__name__)

DATASET_DIR_MODE = 0o755
DATASET_FILE_MODE = 0o644

# Copy objects to disk in 8 MB chunks. Anything streamed is fine; the point is
# that peak memory is this constant and not the object size.
DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024


def _set_mode(path: Path, mode: int) -> None:
    """Best-effort permission normalization for bind-mounted shared paths."""
    os.chmod(path, mode)


def download_object_atomic(
    client: Minio, bucket: str, object_name: str, dest_file: Path, *, mode: int
) -> None:
    """Stream one object to *dest_file*, appearing only once complete.

    Two things this deliberately does not do:

    * It never materializes the object in memory. The previous
      ``dest_file.write_bytes(response.read())`` allocated the whole object as
      a single bytes object, which OOMs the container on pilot exports
      (CEDER is ~500 MB gzipped) and needlessly spikes RSS on ordinary ones.
    * It never writes to *dest_file* directly. The destination sits in a
      directory bind-mounted into running JupyterHub containers, so a
      partially written file is immediately visible to users and reads back
      truncated. Writing to ``<name>.part`` in the same directory and then
      ``os.replace``-ing makes the swap atomic — readers see the old file or
      the new one, never a mixture.
    """
    tmp_file = dest_file.with_name(dest_file.name + ".part")
    response = None
    try:
        response = client.get_object(bucket, object_name)
        with open(tmp_file, "wb") as fh:
            shutil.copyfileobj(response, fh, DOWNLOAD_CHUNK_BYTES)
        _set_mode(tmp_file, mode)
        os.replace(tmp_file, dest_file)
    except BaseException:
        tmp_file.unlink(missing_ok=True)
        raise
    finally:
        if response is not None:
            response.close()
            response.release_conn()


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
    local_dataset_name: str | None = None,
) -> list[str]:
    """Download every file under user_{owner}/{dataset_name}/ into
    {jupyterhub_data_path}/datasets/{target_username}/{local_dataset_name}/.

    The MinIO source path always uses the original ``dataset_name`` (the
    storage convention is unchanged). When ``local_dataset_name`` is provided
    the dataset is materialized on disk under that name instead — this is how
    the dashboard exposes user-renamed datasets in JupyterHub while keeping
    the MinIO layout stable. If omitted it defaults to ``dataset_name``.

    When *overwrite* is ``False``, files that already exist locally are kept
    as-is and only missing files are downloaded.

    Returns the list of filenames that were written.
    """
    local_name = local_dataset_name or dataset_name
    prefix = f"user_{owner}/{dataset_name}/"
    dest_dir = (
        Path(settings.jupyterhub_data_path)
        / "datasets"
        / target_username
        / local_name
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
        download_object_atomic(
            client,
            settings.datasets_bucket,
            obj.object_name,
            dest_file,
            mode=DATASET_FILE_MODE,
        )
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
