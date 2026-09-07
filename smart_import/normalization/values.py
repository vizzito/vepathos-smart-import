"""Coercion de valores y formateo de salida.

El formateo es EXACTO en el sentido de round-trip: un archivo que ya viene en
formato Vepathos tiene que salir identico, sin '22.0' donde decia '22' ni
'-34.58980000001' donde decia '-34.5898'.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import Any

DATETIME_OUT = "%Y-%m-%d %H:%M"
UTC_OUT = "%Y-%m-%dT%H:%M:%SZ"
_AWARE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%d-%m-%Y %H:%M", "%d-%m-%Y", "%Y/%m/%d %H:%M", "%Y/%m/%d",
)
_TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or \
        (isinstance(value, float) and math.isnan(value))


def to_float(value: Any) -> float | None:
    """Numero tolerante a formato europeo/argentino.

    '0,5' -> 0.5      (coma decimal, el default del Excel en es-AR/es-ES)
    '1.234,56' -> 1234.56
    '1,234.56' -> 1234.56
    '$ 1.234' -> 1234
    """
    if is_blank(value):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip()
    s = re.sub(r"[^\d,.\-+eE]", "", s)              # saca simbolos de moneda, unidades
    if not s or s in {"-", "+", ".", ","}:
        return None

    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        # el separador decimal es el que aparece MAS A LA DERECHA
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    elif has_comma:
        tail = s.split(",")[-1]
        # '1,234' con 3 digitos finales es separador de miles; '0,5' es decimal
        s = s.replace(",", "") if (len(tail) == 3 and len(s.split(",")) > 1 and
                                   len(s.split(",")[0]) <= 3 and s.count(",") >= 1
                                   and "." not in s and len(s.replace(",", "")) > 3
                                   and not s.startswith("0,")) else s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    f = to_float(value)
    if f is None:
        return None
    return int(round(f))


def to_str(value: Any) -> str | None:
    if is_blank(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime):
        return value.strftime(DATETIME_OUT)
    return str(value).strip() or None


def _as_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        return dt.strftime(DATETIME_OUT)
    return dt.astimezone(timezone.utc).strftime(UTC_OUT)


def parse_datetime(value: Any) -> datetime | None:
    """Parsea a datetime (aware si el stamp traia offset/Z). No inventa fecha."""
    if is_blank(value):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)

    s = str(value).strip()
    if _TIME_ONLY.match(s):
        return None
    if _AWARE.search(s):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def to_datetime(value: Any) -> str | None:
    """Naive → 'YYYY-MM-DD HH:MM'. Aware/Z → ISO UTC. No inventa fecha si solo hay hora."""
    dt = parse_datetime(value)
    if dt is None:
        return None
    return _as_utc_iso(dt)


def format_number(value: float | int | None) -> str:
    """Repr mas corto que round-trippea. 22.0 -> '22', 0.15 -> '0.15'."""
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


COERCERS = {
    "string": to_str,
    "float": to_float,
    "integer": to_int,
    "datetime": to_datetime,
}


def coerce(value: Any, type_name: str) -> Any:
    return COERCERS.get(type_name, to_str)(value)


def render(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format_number(value)
    return str(value)
