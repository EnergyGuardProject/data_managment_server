"""Manual trigger and status for the nightly pilot exports.

The scheduled runs live in their own container (``python -m app.scheduler``).
This router is the escape hatch for forcing a refresh without waiting for the
schedule — e.g. after a partner backfills their data lake.

For a large partner prefer the CLI in the scheduler container, which keeps the
work out of the API container entirely::

    docker compose exec pilot-export-scheduler python -m app.export_cli CEDER
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.config import settings
from app.dependencies import verify_api_key
from app.models import PilotExportRequest, PilotExportSummary
from app.pilots import PARTNERS, normalize_partner, pilot_file_name, pilot_object_name
from app.services.datalake import disk_free_bytes, export_all

router = APIRouter(prefix="/pilot-export", tags=["pilot-export"])
logger = logging.getLogger(__name__)

_AuthDep = Annotated[str, Depends(verify_api_key)]


def _resolve_targets(requested: list[str] | None) -> list[str]:
    if not requested:
        return list(PARTNERS)
    targets: list[str] = []
    for raw in requested:
        partner = normalize_partner(raw)
        if partner is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Unknown pilot partner '{raw}'. "
                    f"Known partners: {', '.join(PARTNERS)}."
                ),
            )
        targets.append(partner)
    return targets


def _summarize(result) -> PilotExportSummary:
    return PilotExportSummary(
        partner=result.partner,
        ok=result.ok,
        rows=result.rows,
        compressed_bytes=result.compressed_bytes,
        duration_seconds=result.duration_seconds,
        minio_object=result.minio_object,
        local_path=result.local_path,
        errors=result.errors,
    )


@router.post("/run", summary="Force a pilot export now")
def run_pilot_export(
    _key: _AuthDep,
    req: PilotExportRequest,
    response: Response,
    wait: Annotated[
        bool,
        Query(description="Block until the export finishes. Only sensible for "
                          "small partners — CEDER takes far longer than any "
                          "reasonable HTTP timeout."),
    ] = False,
):
    """Export the requested partners (all seven if none are given).

    Concurrency is safe: ``app.services.datalake`` takes a per-partner file
    lock in the shared directory, so a manual trigger that overlaps the
    scheduled run fails fast for that partner instead of two exports fighting
    over the same ``.part`` file.
    """
    targets = _resolve_targets(req.partners)

    if wait:
        # This endpoint is sync, so FastAPI already runs it in a worker thread —
        # the event loop keeps serving other requests while this blocks.
        results = export_all(targets)
        return {"results": [_summarize(r) for r in results]}

    def _background() -> None:
        try:
            export_all(targets)
        except Exception:
            logger.exception("Background pilot export failed")

    threading.Thread(
        target=_background, name="pilot-export-manual", daemon=False
    ).start()

    response.status_code = status.HTTP_202_ACCEPTED
    return {
        "started": targets,
        "detail": "Export running in the background; watch the service logs.",
    }


@router.get("/status", summary="What pilot data is currently on disk")
def pilot_export_status(_key: _AuthDep):
    """Per-partner view of the last successful export, from the shared dir."""
    base = Path(settings.jupyterhub_data_path) / settings.pilot_datasets_prefix
    partners = []
    for partner in PARTNERS:
        path = base / partner / pilot_file_name(partner)
        entry = {
            "partner": partner,
            "object": pilot_object_name(partner),
            "local_path": str(path),
            "present": path.exists(),
        }
        if path.exists():
            stat = path.stat()
            entry["size_bytes"] = stat.st_size
            entry["modified"] = datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat()
        partners.append(entry)
    return {
        "pilot_datasets_prefix": settings.pilot_datasets_prefix,
        "shared_dir": str(base),
        "mount_path_in_jupyterhub": settings.pilot_mount_path,
        "free_bytes": disk_free_bytes(),
        "partners": partners,
    }
