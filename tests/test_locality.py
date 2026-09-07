"""Localidad del depot desde el indice OSM."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from smart_import.geocoding.depot_context import DepotContext
from smart_import.geocoding.locality import fill_depot_from_index, resolve_locality_near
from smart_import.geocoding.osm_index import SCHEMA_SQL


def _tiny_index(tmp_path: Path) -> Path:
    path = tmp_path / "tandil.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    # Tandil ~ -37.32, -59.13
    places = [
        (1, -37.321, -59.133, "Tandil", None, "Buenos Aires", "B7000", "AR"),
        (2, -37.322, -59.134, "Tandil", None, "Buenos Aires", "B7000", "AR"),
        (3, -37.320, -59.132, "Tandil", "Centro", "Buenos Aires", "B7000", "AR"),
        (4, -37.319, -59.131, "Tandil", None, "Buenos Aires", "B7001", "AR"),
    ]
    for i, lat, lon, city, dist, state, post, country in places:
        conn.execute(
            "INSERT INTO places(id, osm_type, osm_id, lat, lon, kind, name, house_number,"
            " street, city, district, state, postcode, country, normalized_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, "node", i, lat, lon, "building", None, "100", "Dufau",
             city, dist, state, post, country, f"100 dufau {city} {post}"),
        )
        conn.execute(
            "INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon)"
            " VALUES (?,?,?,?,?)",
            (i, lat, lat, lon, lon),
        )
    conn.commit()
    conn.close()
    return path


def test_resolve_locality_tandil(tmp_path):
    idx = _tiny_index(tmp_path)
    found = resolve_locality_near(idx, -37.3004, -59.0877, radius_deg=0.05)
    # depot un poco lejos del centro de muestras → ampliar
    found = resolve_locality_near(idx, -37.321, -59.133, radius_deg=0.02)
    assert found.get("city") == "Tandil"
    assert found.get("postcode") in {"B7000", "B7001"}
    assert found.get("region") == "Buenos Aires"


def test_fill_depot_enriquece_calle_sola(tmp_path):
    idx = _tiny_index(tmp_path)
    depot = DepotContext(lat=-37.321, lon=-59.133, max_distance_km=500)
    assert depot.enrichment_tokens() == []  # sin metro hint para Tandil
    filled = fill_depot_from_index(depot, idx, country_slug="argentina")
    assert filled is not None
    assert filled.city == "Tandil"
    assert filled.postcode is None  # CP del entorno del depot no se asume
    assert filled.country == "Argentina"
    q = filled.enrich_address("Dufau 1418")
    assert "Dufau 1418" in q
    assert "Tandil" in q
    assert "Argentina" in q
    assert "7000" not in q and "B7000" not in q


def test_fill_depot_slug_global_no_solo_latam(tmp_path):
    idx = _tiny_index(tmp_path)
    depot = DepotContext(lat=-37.321, lon=-59.133, max_distance_km=500)
    filled = fill_depot_from_index(depot, idx, country_slug="india")
    assert filled is not None
    assert filled.country == "India"


def test_fill_depot_respeta_postcode_explicito(tmp_path):
    idx = _tiny_index(tmp_path)
    depot = DepotContext(
        lat=-37.321, lon=-59.133, postcode="B7000", max_distance_km=500,
    )
    filled = fill_depot_from_index(depot, idx, country_slug="argentina")
    assert filled is not None and filled.postcode == "B7000"
    assert "B7000" in filled.enrich_address("Dufau 1418")
