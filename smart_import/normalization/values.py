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


def to_coordinate(value: Any) -> float | None:
    """Latitud o longitud: un unico separador es SIEMPRE decimal, sea coma o punto.

    `to_float` lee '1,250' como 1250 porque en un monto tres digitos tras la coma son miles. Una
    coordenada no tiene miles: '-37,321' (un Excel es-AR que redondeo a 3 decimales) es -37.321, y
    leerlo como -37321 la manda fuera de rango y la entrega queda sin ubicar. Con los dos separadores
    ('-37.321,5' no existe en la practica) decide `to_float` como siempre.
    """
    if isinstance(value, str):
        s = re.sub(r"[^\d,.\-+eE]", "", value.strip())
        if s.count(",") == 1 and "." not in s:
            value = s.replace(",", ".")
    return to_float(value)


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


# Campos cuyo numero es una coordenada, no una cantidad: ver `to_coordinate`.
COORDINATE_FIELDS = frozenset({"lat", "lng"})


def coerce(value: Any, type_name: str, field: str | None = None) -> Any:
    if field in COORDINATE_FIELDS and type_name == "float":
        return to_coordinate(value)
    return COERCERS.get(type_name, to_str)(value)


def _float_with_decimal(value: Any, decimal: str) -> float | None:
    """Numero con el separador decimal que declaro el usuario: '1.234' con coma decimal es 1234."""
    if is_blank(value) or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^\d,.\-+eE]", "", str(value).strip())
    thousands = "." if decimal == "," else ","
    s = s.replace(thousands, "").replace(decimal, ".")
    try:
        return float(s)
    except ValueError:
        return None


def _datetime_candidates(fmt: str) -> list[str]:
    """Un formato de fecha declarado, con la hora opcional y cualquier separador de fecha."""
    bases = [fmt]
    for sep in ("/", "-", "."):
        for other in ("/", "-", "."):
            if sep != other and sep in fmt:
                bases.append(fmt.replace(sep, other))
    out: list[str] = []
    for base in bases:
        out.append(base)
        if "%H" not in base and "%I" not in base:
            out += [base + " %H:%M", base + " %H:%M:%S", base + "T%H:%M", base + "T%H:%M:%S"]
    return list(dict.fromkeys(out))


def coerce_formatted(value: Any, type_name: str, fmt: str) -> Any:
    """Coercion con el formato que declaro el usuario. Un valor que no cumple el formato da None:
    adivinar con otro formato (dia/mes invertidos) seria peor que no tener el dato."""
    if fmt in ("decimal_comma", "decimal_point") and type_name in ("float", "integer"):
        number = _float_with_decimal(value, "," if fmt == "decimal_comma" else ".")
        if number is None:
            return None
        return int(round(number)) if type_name == "integer" else number
    if type_name == "datetime":
        if is_blank(value):
            return None
        if isinstance(value, datetime):
            return _as_utc_iso(value)
        if isinstance(value, date):
            return _as_utc_iso(datetime(value.year, value.month, value.day))
        s = " ".join(str(value).strip().split())
        s = re.sub(r"(?i)\b([ap])\.?\s?m\.?$", lambda m: m.group(1).upper() + "M", s)
        for candidate in _datetime_candidates(fmt):
            try:
                return _as_utc_iso(datetime.strptime(s, candidate))
            except ValueError:
                continue
        return None
    return coerce(value, type_name)


def render(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format_number(value)
    return str(value)
