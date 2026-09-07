"""IANA timezone → UTC para ventanas horarias.

Si RouteHub manda un timezone (settings del usuario, o el del depot), las horas
naive del dia de servicio se interpretan en esa zona y se emiten en UTC.
Sin timezone no se inventa: se deja la hora naive como hasta ahora.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC_NAME = "UTC"
UTC_OUT = "%Y-%m-%dT%H:%M:%SZ"
NAIVE_OUT = "%Y-%m-%d %H:%M"


def validate_iana(name: str | None) -> str | None:
    """Nombre IANA canonico, o None si falta / no existe (no se inventa)."""
    raw = (name or "").strip()
    if not raw:
        return None
    try:
        ZoneInfo(raw)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return None
    return raw


def resolve_timezone(*candidates: str | None) -> str | None:
    """Primera IANA valida. Precedencia: settings usuario → depot → nada."""
    for candidate in candidates:
        resolved = validate_iana(candidate)
        if resolved:
            return resolved
    return None


def to_utc_iso(local: datetime, tz_name: str) -> str:
    """Naive local en `tz_name` → ISO UTC con Z."""
    aware = local.replace(tzinfo=ZoneInfo(tz_name))
    utc = aware.astimezone(timezone.utc)
    return utc.strftime(UTC_OUT)


def format_window(
    start: datetime,
    end: datetime,
    tz_name: str | None = None,
) -> dict[str, str]:
    """tw_start/tw_end (+ tw_timezone=UTC si se convirtio)."""
    if tz_name:
        return {
            "tw_start": to_utc_iso(start, tz_name),
            "tw_end": to_utc_iso(end, tz_name),
            "tw_timezone": UTC_NAME,
        }
    return {
        "tw_start": start.strftime(NAIVE_OUT),
        "tw_end": end.strftime(NAIVE_OUT),
    }


def convert_naive_stamp(stamp: str, tz_name: str) -> str | None:
    """Convierte un stamp naive (o ya UTC) a ISO UTC. None si no parsea."""
    from ..normalization.values import parse_datetime

    dt = parse_datetime(stamp)
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).strftime(UTC_OUT)
    return to_utc_iso(dt, tz_name)
