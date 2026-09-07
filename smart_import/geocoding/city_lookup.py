"""Centroide de ciudad para elegir PBF / origin del geolocalizador.

Fuente: GeoNames cities15000 (mismo dump que locality_expand_geonames).
Sin archivo → None: no se inventa una ciudad.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from ..normalization.address import iso_from_country_label
from ..resources import fold

_CITIES_NAME = "cities15000.txt"


def _cities_path() -> Path | None:
    env = (os.getenv("SMART_IMPORT_GEONAMES_CITIES") or "").strip()
    if env and Path(env).is_file():
        return Path(env)
    here = Path(__file__).resolve().parents[2] / "data" / "geonames" / _CITIES_NAME
    return here if here.is_file() else None


@lru_cache(maxsize=1)
def _city_index() -> dict[str, list[tuple[int, str, float, float]]]:
    """fold(nombre) → [(poblacion, iso2, lat, lon), …] mayor población primero."""
    path = _cities_path()
    if path is None:
        return {}
    index: dict[str, list[tuple[int, str, float, float]]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 15:
                continue
            name, ascii_name = parts[1], parts[2]
            try:
                lat, lon = float(parts[4]), float(parts[5])
                pop = int(parts[14] or 0)
            except ValueError:
                continue
            iso = (parts[8] or "").upper()
            row = (pop, iso, lat, lon)
            for key in {fold(name), fold(ascii_name)}:
                if not key:
                    continue
                index.setdefault(key, []).append(row)
    for key, rows in index.items():
        rows.sort(key=lambda r: -r[0])
    return index


def lookup_city_centroid(
    city: str | None,
    country: str | None = None,
) -> tuple[float, float] | None:
    """(lat, lon) de la ciudad más poblada que matchee, filtrada por país si hay."""
    name = fold(city or "")
    if not name:
        return None
    rows = _city_index().get(name)
    if not rows:
        return None
    iso = iso_from_country_label(country)
    if iso:
        matched = [r for r in rows if r[1] == iso]
        if matched:
            rows = matched
    _pop, _iso, lat, lon = rows[0]
    return (lat, lon)
