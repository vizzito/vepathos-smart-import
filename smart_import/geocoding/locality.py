"""Resolver ciudad/CP/provincia del depot mirando el indice OSM local.

Sin APIs externas: con lat/lon del depot consultamos places cercanos y tomamos
la moda de city / postcode / state / country. Asi "Dufau 1418" + depot Tandil
pasa a "Dufau 1418, Tandil, B7000, Buenos Aires, Argentina".
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

from ..resources import country_label_from_slug
from .depot_context import DepotContext

# ~5–6 km a lat -37; suficiente para leer tags de manzanas vecinas
_DEFAULT_RADIUS_DEG = 0.05


def _mode(values: list[str | None], *, min_count: int = 2) -> str | None:
    cleaned = [v.strip() for v in values if v and str(v).strip()]
    if not cleaned:
        return None
    counts = Counter(cleaned)
    top, n = counts.most_common(1)[0]
    # Con pocas muestras igual aceptamos la unica evidencia
    if n >= min_count or len(cleaned) <= 3:
        return top
    return top if n >= 1 else None


def resolve_locality_near(
    index_path: str | Path,
    lat: float,
    lon: float,
    *,
    radius_deg: float = _DEFAULT_RADIUS_DEG,
    limit: int = 800,
) -> dict[str, str]:
    """Devuelve {city, region, postcode, country} desde places cercanos al punto."""
    path = Path(index_path)
    if not path.exists():
        return {}
    south, north = lat - radius_deg, lat + radius_deg
    west, east = lon - radius_deg, lon + radius_deg
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            """
            SELECT p.city, p.district, p.state, p.postcode, p.country
            FROM places_rtree r
            JOIN places p ON p.id = r.id
            WHERE r.max_lat >= ? AND r.min_lat <= ?
              AND r.max_lon >= ? AND r.min_lon <= ?
              AND (p.city IS NOT NULL OR p.district IS NOT NULL
                   OR p.state IS NOT NULL OR p.postcode IS NOT NULL
                   OR p.country IS NOT NULL)
            LIMIT ?
            """,
            (south, north, west, east, limit),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        if conn is not None:
            conn.close()

    if not rows:
        return {}

    cities = [r[0] or r[1] for r in rows]  # city o district/barrio
    states = [r[2] for r in rows]
    posts = [r[3] for r in rows]
    countries = [r[4] for r in rows]

    out: dict[str, str] = {}
    if city := _mode(cities, min_count=1):
        out["city"] = city
    if region := _mode(states, min_count=1):
        out["region"] = region
    if postcode := _mode(posts, min_count=1):
        out["postcode"] = postcode
    if country := _mode(countries, min_count=1):
        out["country"] = country
    return out


def fill_depot_from_index(
    depot: DepotContext | None,
    index_path: str | Path,
    *,
    country_slug: str | None = None,
    include_postcode: bool = False,
) -> DepotContext | None:
    """Completa city/region/country del depot si faltan.

    El CP del entorno del depot NO se inyecta por defecto: el deposito puede
    estar fuera de la zona de entrega; ciudad/region/pais si son una pista util
    para desambiguar homonimos al geocodificar.
    """
    if depot is None or depot.origin is None:
        return depot

    lat, lon = depot.origin
    found = resolve_locality_near(index_path, lat, lon)

    country = depot.country
    if not country and country_slug:
        country = country_label_from_slug().get(
            country_slug.strip().lower().replace("_", "-")
        )
    if not country:
        country = found.get("country")

    from .depot_context import normalize_locality_label

    # Preferir valores explicitos del cliente; rellenar huecos desde el indice
    city = normalize_locality_label(depot.city or found.get("city"))
    region = normalize_locality_label(depot.region or found.get("region"))
    country = normalize_locality_label(country)
    # CP solo si el cliente lo mando, o si se pide explicitamente
    postcode = depot.postcode
    if include_postcode and not postcode:
        postcode = found.get("postcode")

    if (city, region, postcode, country) == (
        depot.city, depot.region, depot.postcode, depot.country,
    ):
        return depot

    return DepotContext(
        lat=depot.lat,
        lon=depot.lon,
        city=city,
        region=region,
        postcode=postcode,
        country=country,
        address=depot.address,
        max_distance_km=depot.max_distance_km,
    )
