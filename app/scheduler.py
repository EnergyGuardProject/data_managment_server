"""Nightly pilot-export scheduler.

Runs as its own container (``pilot-export-scheduler`` in docker-compose.yaml)
from the same image as the API, with a different command:

    python -m app.scheduler

Why a separate container rather than scheduling inside the API process:

* A CEDER export is several GB and runs for a long time. In-process it would
  compete with request handling for the GIL and the container's memory budget;
  out-of-process it cannot touch API latency at all.
* Redeploying the API is routine. If the scheduler lived there, every deploy
  would kill a running export halfway through.

Why APScheduler rather than cron:

* The job stays Python, so it reuses this repo's config, MinIO client and
  logging instead of re-deriving them in a shell wrapper.
* Cron expressions come for free, and so does overlap prevention:
  ``max_instances=1`` means a run that overruns its next trigger is skipped
  rather than started twice against the same partner.
* ``coalesce=True`` collapses a backlog of missed triggers into a single run,
  and ``misfire_grace_time`` still lets a job that was due during a short
  outage run once the scheduler is back.
"""

from __future__ import annotations

import logging
import signal
import sys

from datetime import datetime, timedelta

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.config import settings
from app.pilots import PARTNERS
from app.services.datalake import export_age_hours, export_partner

# Nightly order. CEDER is ~127M rows — an order of magnitude larger than any
# other partner — so it takes the first slot and gets the whole night's runway.
# The rest follow in registry order; all of them are minutes, not hours.
EXPORT_ORDER: tuple[str, ...] = ("CEDER",) + tuple(
    p for p in PARTNERS if p != "CEDER"
)

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
logger = logging.getLogger("pilot-export-scheduler")


def _slot(index: int) -> tuple[int, int]:
    """Return the (hour, minute) for the *index*-th partner of the night.

    The seven partners are staggered rather than fired together: they share one
    upstream Postgres, one MinIO and one disk, and running them concurrently
    would just make each slower while multiplying peak load on the data lake.
    """
    start = settings.pilot_export_hour * 60 + settings.pilot_export_minute
    minutes = start + index * settings.pilot_export_stagger_minutes
    return (minutes // 60) % 24, minutes % 60


def _run(partner: str) -> None:
    result = export_partner(partner)
    if not result.ok:
        # Already logged in detail by export_partner; keep the scheduler's own
        # log readable but explicit about which partner failed.
        logger.error(
            "Scheduled export for %s did not fully succeed: %s",
            partner, "; ".join(result.errors) or "unknown error",
        )


def _stale_partners() -> list[tuple[str, str]]:
    """Partners whose export is missing or older than the staleness threshold."""
    stale: list[tuple[str, str]] = []
    for partner in EXPORT_ORDER:
        age = export_age_hours(partner)
        if age is None:
            stale.append((partner, "no export on disk"))
        elif age > settings.pilot_export_max_age_hours:
            stale.append((partner, f"last export was {age:.1f}h ago"))
    return stale


def _schedule_catch_up(scheduler: BlockingScheduler) -> None:
    """Queue one-off exports at startup for partners that need one.

    This is what makes a first deploy — or a restart after the VM was down
    overnight — produce data without waiting for the next nightly slot.

    It is deliberately *conditional*. The container restarts on crash, on
    Docker daemon restart and on every redeploy; an unconditional catch-up
    would re-export CEDER (~127M rows) every one of those times, hammer the
    CARTIF data lake and drag a multi-GB job into the middle of the working
    day. Checking age first means a crash loop or a routine redeploy finds
    fresh files and does nothing.

    The jobs go through the same single-worker executor as the cron jobs, so a
    catch-up can never run alongside a scheduled export.
    """
    if not settings.pilot_export_on_startup:
        logger.info("Startup catch-up disabled (PILOT_EXPORT_ON_STARTUP=false).")
        return

    stale = _stale_partners()
    if not stale:
        logger.info(
            "Startup catch-up: nothing to do, every partner exported within "
            "the last %dh.", settings.pilot_export_max_age_hours,
        )
        return

    run_at = datetime.now() + timedelta(
        seconds=settings.pilot_export_startup_delay_seconds
    )
    for partner, reason in stale:
        scheduler.add_job(
            _run,
            trigger=DateTrigger(run_date=run_at),
            args=[partner],
            id=f"pilot-export-catchup-{partner}",
            name=f"Pilot export (startup catch-up): {partner}",
            replace_existing=True,
        )
        logger.info("Startup catch-up queued for %-8s — %s", partner, reason)


def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(
        # A single worker thread serializes the exports. The staggered cron
        # slots below stop them from being *fired* together; this stops them
        # from *running* together when one overruns its slot — CEDER can easily
        # outlast the 45-minute gap, and seven concurrent COPYs against one
        # Postgres would only make each of them slower. Queued jobs still run,
        # which is why misfire_grace_time has to exceed a full CEDER export.
        executors={"default": ThreadPoolExecutor(max_workers=1)},
        job_defaults={
            # One run per partner at a time — an overrunning CEDER export is
            # never started a second time on top of itself.
            "max_instances": 1,
            # Collapse missed triggers into a single run instead of a burst.
            "coalesce": True,
            "misfire_grace_time": settings.pilot_export_misfire_grace_time,
        },
        timezone=None,  # container local time; see TZ in docker-compose.yaml
    )

    for index, partner in enumerate(EXPORT_ORDER):
        hour, minute = _slot(index)
        scheduler.add_job(
            _run,
            trigger=CronTrigger(hour=hour, minute=minute),
            args=[partner],
            id=f"pilot-export-{partner}",
            name=f"Pilot export: {partner}",
            replace_existing=True,
        )
        logger.info(
            "Scheduled pilot export for %-8s daily at %02d:%02d", partner, hour, minute
        )

    _schedule_catch_up(scheduler)

    return scheduler


def main() -> int:
    if not settings.datalake_password:
        logger.error(
            "DATALAKE_PASSWORD is not set — the scheduler would fail every "
            "run. Refusing to start."
        )
        return 1

    scheduler = build_scheduler()

    def _shutdown(signum, _frame):
        logger.info("Received signal %s, shutting down scheduler …", signum)
        # wait=True lets an export in flight finish rather than leaving a
        # half-written .part file behind.
        scheduler.shutdown(wait=True)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info("Pilot export scheduler started.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
