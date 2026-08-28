"""Registry of the seven EnergyGuard pilot partners.

Each partner has its own database in the CARTIF data lake, and one object per
partner in MinIO under ``<pilot_datasets_prefix>/<PARTNER>/<PARTNER>.csv.gz``.
The dashboard sends the bare partner code (e.g. ``"RDN"``); everything else in
this codebase derives from this table.
"""

from app.config import settings

# Partner code -> data lake database name.
PARTNER_DATABASES: dict[str, str] = {
    "RDN": "TEF1_RDN",
    "CEDER": "TEF2_CEDER",
    "BER": "TEF3_BER",
    "CEA": "TEF4_CEA",
    "CARTIF": "TEF5_CARTIF",
    "REA": "TEF6_REA",
    "ENGREEN": "TEF7_ENGREEN",
}

# Declaration order is also the nightly export order (see app/scheduler.py).
PARTNERS: tuple[str, ...] = tuple(PARTNER_DATABASES)


def normalize_partner(value: str) -> str | None:
    """Return the canonical partner code for *value*, or ``None`` if unknown."""
    if not value:
        return None
    candidate = value.strip().upper()
    return candidate if candidate in PARTNER_DATABASES else None


def pilot_object_name(partner: str) -> str:
    """MinIO object name for a partner's export, inside the datasets bucket."""
    return f"{settings.pilot_datasets_prefix}/{partner}/{partner}.csv.gz"


def pilot_file_name(partner: str) -> str:
    return f"{partner}.csv.gz"
