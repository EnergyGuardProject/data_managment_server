"""Nightly export of each pilot partner's raw time-series table.

Pipeline, per partner, with nothing ever held in memory:

    Postgres COPY … TO STDOUT  ->  gzip  ->  .<P>.csv.gz.part  ->  MinIO fput_object
                                                  |               pilot_datasets/<P>/<P>.csv.gz
                                   streaming CSV reader (batches)
                                                  |
                                    .<P>.parquet.part  ->  os.replace() onto <P>.parquet

The data lake is read once. MinIO keeps a gzipped CSV (the dashboard serves
and previews that key); JupyterHub users get Parquet, converted from that same
local file. Even the smallest partner is too large as CSV for JupyterLab's
viewer, and CEDER is ~9 GB of CSV, while Parquet is typed, a fraction of the
size, and lets pandas read a single sensor without loading the whole file.

The temp files are dot-prefixed, so Jupyter hides them, and live in the *same
directory* as the final name so ``os.replace`` is a same-filesystem rename —
JupyterHub users therefore only ever see the previous complete export or the
new complete export, never a partial one.
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
import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq

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

SOURCE_TABLE = "public.f_tsdata"


def _calendar_to_timestamp(digits: int) -> str:
    """SQL turning a *digits*-long ``calendar_id`` into a ``timestamp``.

    ``calendar_id`` is a packed bigint, and its width differs per partner:
    CEDER and CEA store ``YYYYMMDDHHMMSS`` (14 digits), the others
    ``YYYYMMDDHHMM`` (12). Integer arithmetic plus ``make_timestamp`` is much
    cheaper per row than a ``to_timestamp`` string parse or a join against
    ``d_calendar``, and yields a zone-less timestamp, so the session TimeZone
    cannot shift or DST-gap the values. The source carries no zone, so none is
    invented here.
    """
    seconds = "calendar_id % 100" if digits == 14 else "0"
    base = "calendar_id / 100" if digits == 14 else "calendar_id"
    return (
        f"make_timestamp(({base} / 100000000)::int, "
        f"({base} / 1000000 % 100)::int, ({base} / 10000 % 100)::int, "
        f"({base} / 100 % 100)::int, ({base} % 100)::int, "
        f"({seconds})::double precision)"
    )


# The raw fact table reshaped for people opening the file cold:
# * ts_id is dropped — it is only the fact table's surrogate row key, with gaps
#   and no relation to time; sensor_id + datetime identify a reading.
# * calendar_id is decoded to a real datetime.
# * `corrected` is kept even though it is false everywhere today: it is how the
#   data quality corrector (D3.1 §3.4) labels imputed values, so dropping it
#   would silently mix generated readings with measured ones.
# * Rows are ordered per sensor, then in time. calendar_id has a fixed width
#   within a partner, so ordering by it is ordering by datetime, and it lets
#   Postgres use the (sensor_id, calendar_id, ts_id) index.
EXPORT_SELECT = f"""
SELECT CASE length(calendar_id::text)
            WHEN 14 THEN {_calendar_to_timestamp(14)}
            WHEN 12 THEN {_calendar_to_timestamp(12)}
       END AS datetime,
       sensor_id,
       f_value AS "values",
       -- COPY writes booleans as t/f; spell them out so pandas reads a bool.
       corrected::text AS corrected
FROM {SOURCE_TABLE}
ORDER BY sensor_id, calendar_id
"""
COPY_SQL = f"COPY ({EXPORT_SELECT}) TO STDOUT WITH (FORMAT CSV, HEADER)"

# Column types for the Parquet file. Spelled out rather than inferred: REA's
# sensor ids are all digits ("01000632023001") and would otherwise become
# integers, losing their leading zeros.
PARQUET_SCHEMA = pa.schema([
    ("datetime", pa.timestamp("s")),
    ("sensor_id", pa.string()),
    ("values", pa.float64()),
    ("corrected", pa.bool_()),
])
# CSV bytes parsed per batch; each batch becomes one Parquet row group. Rows
# are sorted by sensor, so a row group's sensor_id min/max statistics let
# pd.read_parquet(filters=...) skip most of the file.
_PARQUET_BLOCK_BYTES = 64 * 1024 * 1024

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

    def __init__(self, gz: gzip.GzipFile, partner: str, gz_raw_fh) -> None:
        self._gz = gz
        self._partner = partner
        self._gz_raw_fh = gz_raw_fh
        self._next_progress = _PROGRESS_INTERVAL_BYTES

    def write(self, data) -> int:
        if isinstance(data, str):
            data = data.encode("utf-8")
        written = self._gz.write(data)
        self._maybe_log_progress()
        return written

    def _maybe_log_progress(self) -> None:
        compressed = self._gz_raw_fh.tell()
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


def _fsync_close(fh) -> None:
    fh.flush()
    os.fsync(fh.fileno())
    fh.close()


def _stream_copy_to_gzip(partner: str, gz_path: Path) -> int:
    """Run the COPY into *gz_path* as gzip. Returns the row count.

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
            # Pin timestamp output to "YYYY-MM-DD HH:MM:SS" whatever the
            # server's default DateStyle is.
            cur.execute("SET DateStyle = 'ISO, YMD'")
            cur.execute("SELECT to_regclass(%s)", (SOURCE_TABLE,))
            if cur.fetchone()[0] is None:
                raise PartnerExportError(
                    f"{PARTNER_DATABASES[partner]} has no {SOURCE_TABLE} "
                    "(partner not onboarded yet)"
                )

            with open(gz_path, "wb") as gz_raw_fh:
                gz = gzip.GzipFile(
                    filename=f"{partner}.csv",
                    mode="wb",
                    fileobj=gz_raw_fh,
                    compresslevel=settings.pilot_export_gzip_level,
                )
                try:
                    cur.copy_expert(COPY_SQL, _GzipSink(gz, partner, gz_raw_fh))
                finally:
                    gz.close()
                _fsync_close(gz_raw_fh)

            # psycopg2 populates rowcount from the COPY command tag.
            return cur.rowcount
    finally:
        conn.close()


def _gzip_csv_to_parquet(gz_path: Path, parquet_path: Path) -> int:
    """Convert the exported gzipped CSV into Parquet. Returns the row count.

    Streams batch by batch, so memory is bounded by one batch (a few hundred
    MB at most) rather than by the table.
    """
    rows = 0
    reader = pa_csv.open_csv(
        pa.input_stream(str(gz_path), compression="gzip"),
        read_options=pa_csv.ReadOptions(block_size=_PARQUET_BLOCK_BYTES),
        convert_options=pa_csv.ConvertOptions(
            column_types=PARQUET_SCHEMA,
            include_columns=PARQUET_SCHEMA.names,
            true_values=["true"],
            false_values=["false"],
        ),
    )
    with open(parquet_path, "wb") as fh:
        with pq.ParquetWriter(fh, PARQUET_SCHEMA, compression="zstd") as writer:
            for batch in reader:
                writer.write_table(
                    pa.Table.from_batches([batch]).cast(PARQUET_SCHEMA)
                )
                rows += batch.num_rows
        _fsync_close(fh)
    return rows


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
    parquet_tmp = dest_dir / f".{final_path.name}.part"
    gz_tmp = dest_dir / f".{canonical}.csv.gz.part"

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
                rows = _stream_copy_to_gzip(canonical, gz_tmp)
                parquet_rows = _gzip_csv_to_parquet(gz_tmp, parquet_tmp)
                if rows is not None and rows >= 0 and parquet_rows != rows:
                    raise PartnerExportError(
                        f"Parquet has {parquet_rows:,} rows but COPY "
                        f"returned {rows:,}"
                    )
            except Exception:
                parquet_tmp.unlink(missing_ok=True)
                gz_tmp.unlink(missing_ok=True)
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
            result.compressed_bytes = gz_tmp.stat().st_size
            _set_mode(parquet_tmp, DATASET_FILE_MODE)

            # ── Destination 1: MinIO ──────────────────────────────────────
            # fput_object streams from disk; it does not read the file in.
            object_name = pilot_object_name(canonical)
            try:
                client = minio_client or get_minio_client()
                client.fput_object(
                    settings.datasets_bucket,
                    object_name,
                    str(gz_tmp),
                    content_type="application/gzip",
                )
                result.minio_object = object_name
            except Exception as exc:
                # A MinIO outage must not cost us the export we just paid for:
                # publish to the shared dir anyway and report the failure.
                logger.error("[%s] MinIO upload failed: %s", canonical, exc)
                result.errors.append(f"MinIO upload failed: {exc}")
            finally:
                gz_tmp.unlink(missing_ok=True)

            # ── Destination 2: shared JupyterHub dir (atomic) ─────────────
            os.replace(parquet_tmp, final_path)
            _set_mode(final_path, DATASET_FILE_MODE)
            result.local_path = str(final_path)
            # Exports used to be published here as CSV (earlier still, gzipped
            # CSV); drop those so users do not find the same data twice.
            for legacy in (f"{canonical}.csv", f"{canonical}.csv.gz"):
                (dest_dir / legacy).unlink(missing_ok=True)

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
