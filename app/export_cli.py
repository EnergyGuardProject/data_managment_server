"""Manual pilot-export trigger.

Forces a refresh without waiting for the nightly schedule. Run it in the
scheduler container so a large export does not land in the API container:

    docker compose exec pilot-export-scheduler python -m app.export_cli REA
    docker compose exec pilot-export-scheduler python -m app.export_cli --all

Exits non-zero if any requested partner failed, so it is usable from a
wrapper script or a CI step.
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.config import settings
from app.pilots import PARTNERS, normalize_partner
from app.services.datalake import disk_free_bytes, export_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.export_cli",
        description="Export pilot datasets from the CARTIF data lake to "
                    "MinIO and the shared JupyterHub directory.",
    )
    parser.add_argument(
        "partners", nargs="*", metavar="PARTNER",
        help=f"Partner codes to export ({', '.join(PARTNERS)}).",
    )
    parser.add_argument(
        "--all", action="store_true", help="Export all seven partners.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    )
    logger = logging.getLogger("pilot-export-cli")

    if args.all:
        targets = list(PARTNERS)
    elif args.partners:
        targets = []
        for raw in args.partners:
            partner = normalize_partner(raw)
            if partner is None:
                parser.error(
                    f"unknown partner '{raw}'; known: {', '.join(PARTNERS)}"
                )
            targets.append(partner)
    else:
        parser.error("give one or more partner codes, or --all")

    logger.info(
        "Exporting %s (%.1f GB free on the shared volume)",
        ", ".join(targets), disk_free_bytes() / (1024 ** 3),
    )

    results = export_all(targets)

    print()
    print(f"{'PARTNER':<10} {'STATUS':<8} {'ROWS':>14} {'SIZE':>12} {'TIME':>10}")
    for r in results:
        size = f"{r.compressed_bytes / (1024**2):.1f} MB" if r.compressed_bytes else "-"
        rows = f"{r.rows:,}" if r.rows is not None else "-"
        print(
            f"{r.partner:<10} {'ok' if r.ok else 'FAILED':<8} "
            f"{rows:>14} {size:>12} {r.duration_seconds:>9.1f}s"
        )
        for err in r.errors:
            print(f"           ! {err}")

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
