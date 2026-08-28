"""Nightly export of each pilot partner's raw time-series table.

Pipeline, per partner, with nothing ever held in memory:

    Postgres COPY … TO STDOUT  ->  gzip  ->  <PARTNER>.csv.gz.part
                                                |
                            +-------------------+-------------------+
                            |                                       |
                   MinIO fput_object                        os.replace() onto
            pilot_datasets/<P>/<P>.csv.gz                   <P>.csv.gz

The single temp file doubles as the upload source and as the atomic-rename
source. It lives in the *same directory* as its final name so ``os.replace``
is a same-filesystem rename — JupyterHub users therefore only ever see the
previous complete export or the new complete export, never a partial one.
That matters because the shared directory is bind-mounted straight into
running singleuser containers: a non-atomic write is visible byte-by-byte in
the Jupyter file browser and produces truncated ``pd.read_csv`` results.

CEDER is ~127M rows / several GB raw, so every stage here streams.
"""

from __future__ import annotations

import errno
import fcntl
import gzip
import logging
import os
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

from app.config import settings
from app.pilots import (
    PARTNER_DATABASES,
    PARTNERS,
    normalize_partner,
    pilot_file_name,
    pilot_object_name,
)
from app.services.minio_client import (
    DATASET_DIR_MODE,
    DATASET_FILE_MODE,
    get_minio_client,
)

logger = logging.getLogger(__name__)

# The full raw fact table, exactly as the dashboard expects it.
EXPORT_COLUMNS = "ts_id, calendar_id, sensor_id, f_value, corrected"
SOURCE_TABLE = "public.f_tsdata"
COPY_SQL = (
    f"COPY (SELECT {EXPORT_COLUMNS} FROM {SOURCE_TABLE}) "
    "TO STDOUT WITH (FORMAT CSV, HEADER)"
)

# Log a progress line roughly every 250 MB of compressed output so a multi-hour
# CEDER run is observable without spamming the log.
_PROGRESS_INTERVAL_BYTES = 250 * 1024 * 1024


class PartnerExportError(RuntimeError):
    """A single partner's export failed. Never aborts the other partners."""


@dataclass
class ExportResult:
    partner: str
    ok: bool = False
    rows: int | None = None
    compressed_bytes: int | None = None
    duration_seconds: float = 0.0
    minio_object: str | None = None
    local_path: str | None = None
    errors: list[str] = field(default_factory=list)


class _GzipSink:
    """Adapter between psycopg2's ``copy_expert`` and a gzip stream.

    psycopg2 hands COPY output to ``write()`` as ``str`` or ``bytes`` depending
    on how it detects the file object, so normalize here rather than depend on
    that detection. Also tracks compressed size for progress logging.
    """

    def __init__(self, gz: gzip.GzipFile, partner: str, raw_fh) -> None:
        self._gz = gz
        self._partner = partner
        self._raw_fh = raw_fh
        self._next_progress = _PROGRESS_INTERVAL_BYTES

    def write(self, data) -> int:
        if isinstance(data, str):
            data = data.encode("utf-8")
        written = self._gz.write(data)
        self._maybe_log_progress()
        return written

    def _maybe_log_progress(self) -> None:
        compressed = self._raw_fh.tell()
        if compressed >= self._next_progress:
            logger.info(
                "[%s] export in progress: %.1f MB compressed so far",
                self._partner,
                compressed / (1024 * 1024),
            )
            while self._next_progress <= compressed:
                self._next_progress += _PROGRESS_INTERVAL_BYTES


def _pilot_base_dir() -> Path:
    """Shared directory the JupyterHub containers mount read-only."""
    return Path(settings.jupyterhub_data_path) / settings.pilot_datasets_prefix


def _set_mode(path: Path, mode: int) -> None:
    """chmod so jovyan (uid 1000) inside the singleuser containers can read."""
    try:
        os.chmod(path, mode)
    except PermissionError as exc:
        logger.warning("chmod %o on %s skipped: %s", mode, path, exc)


@contextmanager
def _partner_lock(partner: str):
    """Stop a manual trigger from colliding with the scheduled run.

    APScheduler's ``max_instances=1`` only guards the scheduler container. The
    lock file lives in the shared directory, so it also covers the API
    container's manual-trigger endpoint and the CLI entrypoint.
    """
    base = _pilot_base_dir()
    base.mkdir(parents=True, exist_ok=True)
    lock_path = base / f".{partner}.lock"
    fh = open(lock_path, "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise PartnerExportError(
                    f"another export for {partner} is already running"
                ) from exc
            raise
        yield
    finally:
        fh.close()


def _connect(partner: str):
    database = PARTNER_DATABASES[partner]
    if not settings.datalake_password:
        raise PartnerExportError(
            "DATALAKE_PASSWORD is not set — refusing to connect to the data lake"
        )
    conn = psycopg2.connect(
        host=settings.datalake_host,
        port=settings.datalake_port,
        user=settings.datalake_user,
        password=settings.datalake_password,
        dbname=database,
        connect_timeout=settings.datalake_connect_timeout,
        application_name="energyguard-dms-pilot-export",
    )
    conn.set_session(readonly=True, autocommit=True)
    return conn


def _stream_copy_to_gzip(partner: str, tmp_path: Path) -> int:
    """Run the COPY into *tmp_path* as gzip. Returns the row count.

    Everything between the server and the file is a stream: psycopg2 pushes
    COPY chunks into the gzip compressor, which pushes into the file. Peak RSS
    is a few hundred KB of buffers regardless of table size.
    """
    conn = _connect(partner)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SET statement_timeout = %s",
                (settings.datalake_statement_timeout_ms,),
            )
            cur.execute("SELECT to_regclass(%s)", (SOURCE_TABLE,))
            if cur.fetchone()[0] is None:
                raise PartnerExportError(
                    f"{PARTNER_DATABASES[partner]} has no {SOURCE_TABLE} "
                    "(partner not onboarded yet)"
                )

            with open(tmp_path, "wb") as raw_fh:
                gz = gzip.GzipFile(
                    filename=pilot_file_name(partner)[: -len(".gz")],
                    mode="wb",
                    fileobj=raw_fh,
                    compresslevel=settings.pilot_export_gzip_level,
                )
                try:
                    cur.copy_expert(COPY_SQL, _GzipSink(gz, partner, raw_fh))
                finally:
                    gz.close()
                raw_fh.flush()
                os.fsync(raw_fh.fileno())

            # psycopg2 populates rowcount from the COPY command tag.
            return cur.rowcount
    finally:
        conn.close()


def export_partner(partner: str, *, minio_client=None) -> ExportResult:
    """Export one partner to MinIO and the shared JupyterHub directory.

    Raises nothing for ordinary failures — inspect ``ExportResult.ok`` — so a
    caller looping over partners cannot be derailed by one bad partner.
    """
    canonical = normalize_partner(partner)
    if canonical is None:
        return ExportResult(
            partner=partner, errors=[f"Unknown partner '{partner}'"]
        )

    result = ExportResult(partner=canonical)
    started = time.monotonic()

    dest_dir = _pilot_base_dir() / canonical
    final_path = dest_dir / pilot_file_name(canonical)
    tmp_path = final_path.with_name(final_path.name + ".part")

    try:
        with _partner_lock(canonical):
            dest_dir.mkdir(parents=True, exist_ok=True)
            _set_mode(_pilot_base_dir(), DATASET_DIR_MODE)
            _set_mode(dest_dir, DATASET_DIR_MODE)

            logger.info(
                "[%s] exporting %s from %s …",
                canonical, SOURCE_TABLE, PARTNER_DATABASES[canonical],
            )
            try:
                rows = _stream_copy_to_gzip(canonical, tmp_path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                # Leave no empty partner directory behind on a first-time
                # failure: an empty folder in the file browser reads as "this
                # dataset is empty", a missing one as "not available yet".
                # A previous successful export is still in here, so only
                # remove the directory when it is genuinely empty.
                try:
                    dest_dir.rmdir()
                except OSError:
                    pass
                raise

            result.rows = rows if rows is not None and rows >= 0 else None
            result.compressed_bytes = tmp_path.stat().st_size
            _set_mode(tmp_path, DATASET_FILE_MODE)

            # ── Destination 1: MinIO ──────────────────────────────────────
            # fput_object streams from disk; it does not read the file in.
            object_name = pilot_object_name(canonical)
            try:
                client = minio_client or get_minio_client()
                client.fput_object(
                    settings.datasets_bucket,
                    object_name,
                    str(tmp_path),
                    content_type="application/gzip",
                )
                result.minio_object = object_name
            except Exception as exc:
                # A MinIO outage must not cost us the export we just paid for:
                # publish to the shared dir anyway and report the failure.
                logger.error("[%s] MinIO upload failed: %s", canonical, exc)
                result.errors.append(f"MinIO upload failed: {exc}")

            # ── Destination 2: shared JupyterHub dir (atomic) ─────────────
            os.replace(tmp_path, final_path)
            _set_mode(final_path, DATASET_FILE_MODE)
            result.local_path = str(final_path)

            result.ok = not result.errors
    except PartnerExportError as exc:
        logger.error("[%s] export failed: %s", canonical, exc)
        result.errors.append(str(exc))
    except Exception as exc:
        logger.exception("[%s] export failed", canonical)
        result.errors.append(f"{exc.__class__.__name__}: {exc}")
    finally:
        result.duration_seconds = time.monotonic() - started

    if result.ok or result.local_path:
        logger.info(
            "[%s] export finished in %.1fs — %s rows, %.1f MB compressed -> %s",
            canonical,
            result.duration_seconds,
            f"{result.rows:,}" if result.rows is not None else "?",
            (result.compressed_bytes or 0) / (1024 * 1024),
            result.minio_object or "(MinIO upload failed)",
        )
    else:
        logger.error(
            "[%s] export aborted after %.1fs: %s",
            canonical, result.duration_seconds, "; ".join(result.errors),
        )
    return result


def export_all(partners: list[str] | None = None) -> list[ExportResult]:
    """Export several partners sequentially. One failure never stops the rest."""
    targets = list(partners) if partners else list(PARTNERS)
    results: list[ExportResult] = []
    client = get_minio_client()
    for partner in targets:
        results.append(export_partner(partner, minio_client=client))
    ok = sum(1 for r in results if r.ok)
    logger.info("Pilot export batch complete: %d/%d succeeded", ok, len(results))
    return results


def disk_free_bytes() -> int:
    """Free space on the shared volume — useful context when an export fails."""
    base = _pilot_base_dir()
    base.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(base).free


def last_export_at(partner: str) -> datetime | None:
    """When *partner* was last exported, or ``None`` if it never was.

    Read from the published file's mtime rather than any separate bookkeeping:
    the file only gets that name via ``os.replace`` at the end of a successful
    export, so its mtime cannot describe a partial or failed one.
    """
    path = _pilot_base_dir() / partner / pilot_file_name(partner)
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except FileNotFoundError:
        return None


def export_age_hours(partner: str) -> float | None:
    exported_at = last_export_at(partner)
    if exported_at is None:
        return None
    return (datetime.now(timezone.utc) - exported_at).total_seconds() / 3600
