"""Sanity de lat/lng de un corpus vs el país que dice ser.

No hay shapefile de costas: un punto en el océano Índico con país=ES se
detecta igual porque **no cae en el bbox de España**. Si al invertir lat y
lng entra, es el swap GeoJSON [lat,lon] (caso `es/25829`).

No pisa Chile Huara ni Radom: esos puntos SÍ están en el país; el fallo
ahí es índice de capital vs OA countrywide.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from ..normalization.values import to_float
from ..resources import country_label_from_slug, fold, load_json, phone_region_country_map

_BOUNDS_FILE = "pbf_country_bounds.json"
_BOUNDS_ENV = "SMART_IMPORT_PBF_BOUNDS_PATH"
_SPLIT = re.compile(r"[^a-zA-Z0-9]+")

#: Null Island: OA a veces publica 0,0 (fila plantilla, no una dirección).
NULL_ISLAND_ABS = 1e-5

#: fracción del slice con swap → no llamar osmium (extract de océano)
SWAP_SKIP_FRAC = 0.50
#: fuera del país y el swap NO los salva (otro hemisferio / ISO mal)
OUTSIDE_SKIP_FRAC = 0.80


def _parse_box(row: dict) -> tuple[float, float, float, float] | None:
    try:
        return (float(row["n"]), float(row["s"]), float(row["e"]), float(row["w"]))
    except (KeyError, TypeError, ValueError):
        return None


@lru_cache(maxsize=1)
def _country_boxes() -> dict[str, tuple[float, float, float, float]]:
    """Solo países (no CCAA/madrid): el corpus Spain es nacional."""
    raw = load_json(_BOUNDS_FILE, _BOUNDS_ENV).get("countries") or {}
    out: dict[str, tuple[float, float, float, float]] = {}
    for slug, row in raw.items():
        if not isinstance(row, dict):
            continue
        box = _parse_box(row)
        if box is None:
            continue
        keys = [slug]
        keys.extend(str(a) for a in (row.get("aliases") or ()) if str(a).strip())
        if row.get("label"):
            keys.append(str(row["label"]))
        for key in keys:
            folded = fold(key).replace(" ", "-")
            if folded:
                out.setdefault(folded, box)
    return out


def is_null_island(lat: float, lon: float) -> bool:
    return abs(lat) < NULL_ISLAND_ABS and abs(lon) < NULL_ISLAND_ABS


def usable_truth_coord(lat: float | None, lon: float | None) -> bool:
    """Coordenada que puede elegir índice / medir error. 0,0 no."""
    if lat is None or lon is None:
        return False
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    return not is_null_island(lat, lon)


def in_country_box(
    lat: float, lon: float,
    box: tuple[float, float, float, float],
) -> bool:
    north, south, east, west = box
    return south <= lat <= north and west <= lon <= east


def bounds_for_country(hint: str | None) -> tuple[float, float, float, float] | None:
    """ISO-2, slug (`spain`) o label (`Spain`). Varios tokens: el primero que mate."""
    raw = (hint or "").strip()
    if not raw:
        return None
    boxes = _country_boxes()
    labels = country_label_from_slug()
    iso_map = phone_region_country_map()

    def _lookup(part: str) -> tuple[float, float, float, float] | None:
        token = (part or "").strip()
        if not token:
            return None
        folded = fold(token).replace(" ", "-")
        if folded in boxes:
            return boxes[folded]
        if len(token) == 2 and token.isalpha():
            name = iso_map.get(token.upper())
            if name:
                found = _lookup(name)
                if found:
                    return found
        want = fold(token)
        for slug, label in labels.items():
            if fold(label) == want or fold(slug) == want:
                key = fold(slug).replace(" ", "-")
                if key in boxes:
                    return boxes[key]
        return None

    found = _lookup(raw)
    if found:
        return found
    for part in _SPLIT.split(raw):
        found = _lookup(part)
        if found:
            return found
    return None


def classify_latlng(
    lat: float, lon: float, country_hint: str | None,
) -> str:
    """`ok` | `swapped` | `outside` | `unknown` (sin bbox de país)."""
    if not usable_truth_coord(lat, lon):
        return "outside"
    box = bounds_for_country(country_hint)
    if box is None:
        return "unknown"
    if in_country_box(lat, lon, box):
        return "ok"
    if in_country_box(lon, lat, box):
        return "swapped"
    return "outside"


def apply_country_coord_fix(
    lat: float, lon: float, country_hint: str | None,
) -> tuple[float, float, str | None]:
    """Si es swap evidente, intercambia. Si no, deja el punto."""
    if classify_latlng(lat, lon, country_hint) != "swapped":
        return lat, lon, None
    return lon, lat, "swapped_lat_lng"


@dataclass(frozen=True)
class CoordAudit:
    usable: int
    ok: int
    swapped: int
    outside: int
    unknown: int
    skip_reason: str | None = None


def audit_truth_coords(
    filas: list[dict],
    columnas: dict[str, str],
    country_hint: str | None,
) -> CoordAudit:
    """Cuenta ok/swap/fuera. skip_reason si osmium cortaría océano."""
    ok = swapped = outside = unknown = 0
    lat_col, lng_col = columnas.get("lat"), columnas.get("lng")
    if not lat_col or not lng_col:
        return CoordAudit(0, 0, 0, 0, 0, skip_reason="sin columnas lat/lng")
    for fila in filas:
        lat = to_float(fila.get(lat_col))
        lon = to_float(fila.get(lng_col))
        if lat is None or lon is None:
            continue
        kind = classify_latlng(lat, lon, country_hint)
        if kind == "ok":
            ok += 1
        elif kind == "swapped":
            swapped += 1
        elif kind == "outside":
            outside += 1
        else:
            unknown += 1
    usable = ok + swapped + outside + unknown
    skip = None
    if usable and country_hint:
        if swapped / usable >= SWAP_SKIP_FRAC:
            skip = (
                f"lat/lng invertidos ({swapped}/{usable} fuera del país; "
                f"al swapear entran). No es el PBF"
            )
        elif outside / usable >= OUTSIDE_SKIP_FRAC:
            skip = (
                f"coords fuera del bbox del país ({outside}/{usable}). "
                f"¿país mal etiquetado?"
            )
    return CoordAudit(
        usable=usable, ok=ok, swapped=swapped, outside=outside,
        unknown=unknown, skip_reason=skip,
    )
