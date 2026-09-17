"""Mapeo manual con unidad y formato: `{campo, unidad, formato}`.

El mapeo manual siempre fue `{columna: campo}`. Renombrar no alcanza cuando la columna viene en otra
unidad (libras, pulgadas, litros) o en otro formato (fechas mes/día, coma decimal): la inferencia por
nombre de columna sólo reconoce algunos casos ('peso lb', 'm3'). Acá el usuario lo dice explícito, y
lo que dice gana sobre la inferencia.

Acepta el valor de cada columna como:
- `"weight_kg"` o `None`, igual que antes;
- un objeto con las claves en español, inglés o portugués:
  `{"campo" | "field" | "target", "unidad" | "unit" | "unidade", "formato" | "format"}`.

Las unidades y los formatos también se aceptan en los tres idiomas ("libras", "pounds", "libras";
"pulgadas", "inches", "polegadas"; "dd/mm/aaaa", "mm/dd/yyyy", "dia primero", "month first").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from ..schemas import normalize_key

FIELD_KEYS = ("field", "campo", "target", "destino")
UNIT_KEYS = ("unit", "unidad", "unidade")
FORMAT_KEYS = ("format", "formato")


class ColumnSpecError(ValueError):
    """Un `{campo, unidad, formato}` que no se puede aplicar. El mensaje dice qué se acepta."""


@dataclass(frozen=True)
class ColumnSpec:
    field: str | None
    unit: str | None = None          # canonico: kg, g, lb, oz, cm, mm, m, in, ft, cm3, m3, l, ml, ft3, in3, ...
    format: str | None = None        # canonico: strptime ('%m/%d/%Y %H:%M') o 'decimal_comma' / 'decimal_point'


# ---------- unidades ----------

#: dimension de cada campo del schema que tiene unidad, y su unidad canonica
FIELD_DIMENSION: dict[str, str] = {
    "weight_kg": "weight",
    "length_cm": "length", "width_cm": "length", "height_cm": "length",
    "volume_cm3": "volume",
    "value_cents": "currency",
    "service_time_min": "duration",
}

#: factor para llevar 1 unidad a la canonica del campo
_FACTORS: dict[str, dict[str, float]] = {
    "weight": {"kg": 1.0, "g": 0.001, "lb": 0.45359237, "oz": 0.028349523125, "t": 1000.0},
    "length": {"cm": 1.0, "mm": 0.1, "m": 100.0, "in": 2.54, "ft": 30.48},
    "volume": {"cm3": 1.0, "m3": 1_000_000.0, "l": 1000.0, "ml": 1.0, "ft3": 28316.846592, "in3": 16.387064},
    "currency": {"cents": 1.0, "units": 100.0},
    "duration": {"min": 1.0, "s": 1 / 60, "h": 60.0},
}

#: alias (ya normalizados con normalize_key) -> unidad canonica, en es / en / pt
_UNIT_ALIASES: dict[str, dict[str, str]] = {
    "weight": {
        **dict.fromkeys(["kg", "kgs", "kilo", "kilos", "kilogramo", "kilogramos", "kilogram", "kilograms",
                         "kilogramme", "quilo", "quilos", "quilograma", "quilogramas"], "kg"),
        **dict.fromkeys(["g", "gr", "grs", "gramo", "gramos", "gram", "grams", "gramme", "grama", "gramas"], "g"),
        **dict.fromkeys(["lb", "lbs", "libra", "libras", "pound", "pounds"], "lb"),
        **dict.fromkeys(["oz", "onza", "onzas", "ounce", "ounces", "onca", "oncas"], "oz"),
        **dict.fromkeys(["t", "tn", "ton", "tons", "tonelada", "toneladas", "tonne", "tonnes"], "t"),
    },
    "length": {
        **dict.fromkeys(["cm", "cms", "centimetro", "centimetros", "centimeter", "centimeters",
                         "centimetre", "centimetres"], "cm"),
        **dict.fromkeys(["mm", "milimetro", "milimetros", "millimeter", "millimeters", "millimetre",
                         "millimetres"], "mm"),
        **dict.fromkeys(["m", "mt", "mts", "metro", "metros", "meter", "meters", "metre", "metres"], "m"),
        **dict.fromkeys(["in", "inch", "inches", "pulgada", "pulgadas", "polegada", "polegadas"], "in"),
        **dict.fromkeys(["ft", "foot", "feet", "pie", "pies", "pe", "pes"], "ft"),
    },
    "volume": {
        **dict.fromkeys(["cm3", "cc", "centimetro cubico", "centimetros cubicos", "cubic centimeter",
                         "cubic centimeters", "cubic centimetre", "cubic centimetres"], "cm3"),
        **dict.fromkeys(["m3", "cbm", "metro cubico", "metros cubicos", "cubic meter", "cubic meters",
                         "cubic metre", "cubic metres"], "m3"),
        **dict.fromkeys(["l", "lt", "lts", "litro", "litros", "liter", "liters", "litre", "litres"], "l"),
        **dict.fromkeys(["ml", "mililitro", "mililitros", "milliliter", "milliliters", "millilitre",
                         "millilitres"], "ml"),
        **dict.fromkeys(["ft3", "cubic foot", "cubic feet", "pie cubico", "pies cubicos", "pe cubico",
                         "pes cubicos"], "ft3"),
        **dict.fromkeys(["in3", "cubic inch", "cubic inches", "pulgada cubica", "pulgadas cubicas",
                         "polegada cubica", "polegadas cubicas"], "in3"),
    },
    "currency": {
        **dict.fromkeys(["cents", "cent", "centavos", "centavo", "centimos", "centimo", "minor"], "cents"),
        **dict.fromkeys(["units", "unit", "unidades", "unidad", "unidade", "major", "pesos", "dolares",
                         "dollars", "reais", "euros", "moneda", "moeda", "currency"], "units"),
    },
    "duration": {
        **dict.fromkeys(["min", "mins", "minuto", "minutos", "minute", "minutes"], "min"),
        **dict.fromkeys(["s", "seg", "segs", "segundo", "segundos", "second", "seconds", "sec", "secs"], "s"),
        **dict.fromkeys(["h", "hs", "hr", "hrs", "hora", "horas", "hour", "hours"], "h"),
    },
}


def canonical_unit(field: str, unit: Any) -> str:
    """'Libras' para weight_kg -> 'lb'. Error claro si el campo no lleva unidad o la unidad no aplica."""
    dimension = FIELD_DIMENSION.get(field)
    if dimension is None:
        raise ColumnSpecError(
            f"el campo '{field}' no lleva unidad (con unidad: {', '.join(sorted(FIELD_DIMENSION))})")
    key = normalize_key(unit)
    canonical = _UNIT_ALIASES[dimension].get(key)
    if canonical is None:
        accepted = ", ".join(_FACTORS[dimension])
        raise ColumnSpecError(f"unidad '{unit}' no reconocida para '{field}' (acepta: {accepted})")
    return canonical


def unit_factor(field: str, unit: str) -> float:
    return _FACTORS[FIELD_DIMENSION[field]][unit]


# ---------- formatos ----------

DECIMAL_COMMA = "decimal_comma"
DECIMAL_POINT = "decimal_point"

_NUMBER_FORMATS: dict[str, str] = {
    **dict.fromkeys(["decimal comma", "coma decimal", "virgula decimal", "coma", "comma", "virgula",
                     "1 234 56", "1234 56"], DECIMAL_COMMA),
    **dict.fromkeys(["decimal point", "decimal dot", "punto decimal", "ponto decimal", "punto", "point",
                     "dot", "ponto"], DECIMAL_POINT),
}
#: '1.234,56' / '1,234.56' se escriben con puntuacion: se reconocen antes de normalizar
_NUMBER_EXAMPLES = {"1.234,56": DECIMAL_COMMA, "1234,56": DECIMAL_COMMA,
                    "1,234.56": DECIMAL_POINT, "1234.56": DECIMAL_POINT}

_DATE_PRESETS: dict[str, str] = {
    **dict.fromkeys(["dia primero", "dia primeiro", "day first", "dmy", "europeo", "european"], "%d/%m/%Y"),
    **dict.fromkeys(["mes primero", "mes primeiro", "month first", "mdy", "us", "americano", "american"],
                    "%m/%d/%Y"),
    **dict.fromkeys(["iso", "ano primero", "ano primeiro", "year first", "ymd"], "%Y-%m-%d"),
}

#: tokens de un patron de fecha/hora -> strptime. 'mm' se decide por contexto (mes o minutos).
_DATE_TOKENS = [
    ("yyyy", "%Y"), ("aaaa", "%Y"), ("yy", "%y"), ("aa", "%y"),
    ("dd", "%d"), ("d", "%d"),
    ("hh", "%H"), ("h", "%H"),
    ("ss", "%S"),
]


def _date_pattern_to_strptime(pattern: str) -> str | None:
    """'DD/MM/AAAA hh:mm a.m.' -> '%d/%m/%Y %I:%M %p'. None si no parece un patron de fecha u hora."""
    text = pattern.strip()
    lower = text.lower()
    twelve_hour = bool(re.search(r"\b(am|pm|a\.?\s?m\.?|p\.?\s?m\.?)\s*$", lower))
    if twelve_hour:
        lower = re.sub(r"\s*(am/pm|a\.?\s?m\.?/p\.?\s?m\.?|am|pm|a\.?\s?m\.?|p\.?\s?m\.?)\s*$", "", lower)
    out: list[str] = []
    i = 0
    seen_hour = False
    tokens = 0
    while i < len(lower):
        if lower.startswith("mm", i):
            # minutos si viene despues de la hora ('hh:mm'); si no, mes
            out.append("%M" if seen_hour and out and out[-1] == ":" else "%m")
            i += 2
            tokens += 1
            continue
        if lower[i] == "m" and not lower.startswith("mm", i):
            out.append("%M" if seen_hour and out and out[-1] == ":" else "%m")
            i += 1
            tokens += 1
            continue
        for token, directive in _DATE_TOKENS:
            if lower.startswith(token, i):
                if directive == "%H":
                    seen_hour = True
                    directive = "%I" if twelve_hour else "%H"
                out.append(directive)
                i += len(token)
                tokens += 1
                break
        else:
            ch = lower[i]
            if ch.isalpha():
                return None
            out.append(ch)
            i += 1
    if tokens < 2:
        return None
    fmt = "".join(out)
    if not all(d in fmt for d in ("%d", "%m")) or not ("%Y" in fmt or "%y" in fmt):
        # Sin dia, mes y año no hay fecha: una hora sola no se completa con una fecha inventada.
        return None
    return fmt + " %p" if twelve_hour else fmt


def canonical_format(field: str, field_type: str, fmt: Any) -> str:
    """Formato del usuario -> formato aplicable al tipo del campo."""
    raw = str(fmt).strip()
    if not raw:
        raise ColumnSpecError("formato vacio")
    if field_type == "datetime":
        preset = _DATE_PRESETS.get(normalize_key(raw))
        if preset:
            return preset
        strptime = _date_pattern_to_strptime(raw)
        if strptime:
            return strptime
        raise ColumnSpecError(
            f"formato de fecha '{raw}' no reconocido para '{field}' "
            "(ej.: 'dd/mm/aaaa hh:mm', 'mm/dd/yyyy', 'día primero', 'month first', 'iso')")
    if field_type in {"float", "integer"}:
        found = _NUMBER_EXAMPLES.get(raw.replace(" ", "")) or _NUMBER_FORMATS.get(normalize_key(raw))
        if found:
            return found
        raise ColumnSpecError(
            f"formato numerico '{raw}' no reconocido para '{field}' "
            "(ej.: 'coma decimal', 'decimal point', '1.234,56', '1,234.56')")
    raise ColumnSpecError(f"el campo '{field}' ({field_type}) no acepta formato")


# ---------- lectura del valor del mapeo ----------

def _pick(value: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    lowered = {normalize_key(k): v for k, v in value.items()}
    for key in keys:
        if key in lowered:
            return lowered[key]
    return None


def parse_column_spec(value: Any, field_types: Mapping[str, str]) -> ColumnSpec:
    """Un valor del mapeo manual -> ColumnSpec validado contra el schema (`field_types`: campo -> tipo)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ColumnSpec(field=None)
    if isinstance(value, str):
        return ColumnSpec(field=value.strip())
    if not isinstance(value, Mapping):
        raise ColumnSpecError("cada columna mapea a un campo (texto) o a {campo, unidad, formato}")
    known = {normalize_key(k) for k in (*FIELD_KEYS, *UNIT_KEYS, *FORMAT_KEYS)}
    unknown = [k for k in value if normalize_key(k) not in known]
    if unknown:
        raise ColumnSpecError(
            f"claves desconocidas {unknown}: use campo | field, unidad | unit | unidade, formato | format")
    field = _pick(value, FIELD_KEYS)
    if field is None or (isinstance(field, str) and not field.strip()):
        return ColumnSpec(field=None)
    field = str(field).strip()
    if field not in field_types:
        raise ColumnSpecError(f"campo '{field}' no existe en el schema")
    unit_raw = _pick(value, UNIT_KEYS)
    format_raw = _pick(value, FORMAT_KEYS)
    unit = canonical_unit(field, unit_raw) if unit_raw not in (None, "") else None
    fmt = canonical_format(field, field_types[field], format_raw) if format_raw not in (None, "") else None
    return ColumnSpec(field=field, unit=unit, format=fmt)
