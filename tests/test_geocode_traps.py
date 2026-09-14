"""Trampas geográficas congeladas — regresión por patrón (homónimos CABA).

Correr:  pytest -q -m geo_trap
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from smart_import.config import Config
from smart_import.geocoding.address import normalize_text, parse
from smart_import.geocoding.base import STATUS_LOW, STATUS_MATCHED, STATUS_NOT_FOUND
from smart_import.geocoding.depot_context import DepotContext
from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder
from smart_import.geocoding.runner import run as geocode_run
from smart_import.geocoding.scoring import Candidate, haversine_km, score
from smart_import.geocoding.scoring import (
    _candidate_in_caba,
    _candidate_province_only_ba,
    _locality_score,
    _query_wants_caba,
)
from smart_import.geocoding.osm_index import SCHEMA_SQL
from tests.conftest import ROOT

pytestmark = pytest.mark.geo_trap

TRAPS = ROOT / "examples" / "geocode-truth" / "traps" / "caba_homonyms.json"
OA_COMPOUND = ROOT / "examples" / "geocode-truth" / "traps" / "caba_oa_compound.json"

# Santa Fe 2500 y Caseros 1800: homónimo en provincia + pin correcto en CABA
HOMONYM_PLACES = [
    ("node", 101, -34.5945841, -58.4022543, "building", None, "2500", "Avenida Santa Fe",
     "Ciudad Autónoma de Buenos Aires", "2500 avenida santa fe ciudad autonoma de buenos aires c1425"),
    ("node", 102, -34.4938614, -58.4980751, "building", None, "2500", "Avenida Santa Fe",
     "Martínez", "2500 avenida santa fe martinez buenos aires"),
    # CABA sin 1800 exacto (OSM real): solo 1799/1801; provincia sí tiene 1800.
    ("node", 103, -34.6333112, -58.3875531, "building", None, "1799", "Avenida Caseros",
     "Ciudad Autónoma de Buenos Aires", "1799 avenida caseros ciudad autonoma de buenos aires"),
    ("node", 106, -34.6333678, -58.3877794, "building", None, "1801", "Avenida Caseros",
     "Ciudad Autónoma de Buenos Aires", "1801 avenida caseros ciudad autonoma de buenos aires"),
    ("node", 104, -34.52327, -58.48984, "building", None, "1800", "Avenida Caseros",
     "La Matanza", "1800 avenida caseros la matanza buenos aires"),
    ("node", 105, -34.6040186, -58.3844481, "building", None, "1234", "Avenida Corrientes",
     "Ciudad Autónoma de Buenos Aires", "1234 avenida corrientes ciudad autonoma de buenos aires"),
]


@pytest.fixture(scope="module")
def homonym_index(tmp_path_factory):
    path = tmp_path_factory.mktemp("traps") / "homonym.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    for osm_type, osm_id, lat, lon, kind, name, num, street, city, texto in HOMONYM_PLACES:
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, kind, name, house_number,"
            " street, city, district, state, postcode, country, normalized_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,NULL,'Buenos Aires',NULL,'AR',?)",
            (osm_type, osm_id, lat, lon, kind, name, num, street, city,
             normalize_text(texto)),
        )
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) "
                 "SELECT id, normalized_text FROM places")
    conn.execute("INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon) "
                 "SELECT id, lat, lat, lon, lon FROM places")
    conn.commit()
    conn.close()
    return path


def _load_traps() -> dict:
    return json.loads(TRAPS.read_text(encoding="utf-8"))


def test_trap_file_tiene_casos():
    data = _load_traps()
    assert len(data["cases"]) >= 2
    assert data["depot"]["lat"]


def _load_oa_traps() -> dict:
    return json.loads(OA_COMPOUND.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "case_id",
    ["calderon_pedro_3645", "flores_venancio_185", "pena_david_dr_4256"],
)
def test_oa_compound_trap_parse_y_enhance(case_id):
    from smart_import.geocoding.query import build_geocode_query

    data = _load_oa_traps()
    case = next(c for c in data["cases"] if c["id"] == case_id)
    depot = DepotContext(
        lat=data["depot"]["lat"],
        lon=data["depot"]["lon"],
        city=data["depot"].get("city"),
        country=data["depot"].get("country"),
        address=data["depot"].get("address"),
        max_distance_km=500.0,
    )
    parsed = parse(case["address"])
    road = (parsed.road or "").lower()
    for token in case.get("expect_road_contains") or ():
        assert token.lower() in road, f"{case_id}: road={parsed.road!r}"
    sent = build_geocode_query(case["address"], depot=depot, enhance=True).lower()
    for token in case.get("expect_road_contains") or ():
        assert token.lower() in sent, f"{case_id}: sent={sent!r}"
    assert parsed.house_number, case_id


def test_query_wants_caba_con_ciudad_autonoma():
    q = "Av. Santa Fe 2500, Ciudad Autónoma de Buenos Aires, C1425"
    assert _query_wants_caba(q) is True


def test_locality_penaliza_martinez_si_query_pide_caba():
    text = "Av. Santa Fe 2500, Ciudad Autónoma de Buenos Aires, C1425"
    caba = Candidate(
        1, -34.59, -58.40, "node", None, "2500", "Avenida Santa Fe",
        "Ciudad Autónoma de Buenos Aires", None, "Buenos Aires", None, "AR", "x")
    mtz = Candidate(
        2, -34.49, -58.49, "node", None, "2500", "Avenida Santa Fe",
        "Martínez", None, "Buenos Aires", None, "AR", "y")
    assert _candidate_in_caba(caba) is True
    assert _candidate_province_only_ba(mtz) is True
    assert _locality_score(text, caba) == 1.0
    assert _locality_score(text, mtz) == 0.0


def test_geocoder_elige_santa_fe_caba_no_martinez(homonym_index):
    cfg = Config.from_env()
    origin = (-34.598, -58.416)
    g = LocalOSMGeocoder(
        homonym_index,
        match_threshold=cfg.match_threshold,
        low_threshold=cfg.low_confidence_threshold,
        street_level_floor=cfg.geocode_street_level_floor,
        street_match_min=cfg.geocode_street_match_min,
        review_band=cfg.geocode_review_band,
        valid_band=cfg.geocode_valid_band,
        soft_reject=cfg.geocode_soft_reject,
        soft_reject_min=cfg.geocode_soft_reject_min,
    )
    query = "Av. Santa Fe 2500, Ciudad Autónoma de Buenos Aires, C1425, Argentina"
    r = g.geocode(query, origin=origin)
    g.close()
    assert r.status == STATUS_MATCHED
    assert r.lat is not None and r.lon is not None
    err = haversine_km(r.lat, r.lon, -34.5945841, -58.4022543) * 1000
    assert err < 500, f"pin a {err:.0f} m del truth CABA"
    assert haversine_km(origin[0], origin[1], r.lat, r.lon) < 15


def test_geocoder_elige_caseros_caba_no_provincia(homonym_index):
    cfg = Config.from_env()
    origin = (-34.598, -58.416)
    g = LocalOSMGeocoder(
        homonym_index,
        match_threshold=cfg.match_threshold,
        low_threshold=cfg.low_confidence_threshold,
        street_level_floor=cfg.geocode_street_level_floor,
        street_match_min=cfg.geocode_street_match_min,
        review_band=cfg.geocode_review_band,
        valid_band=cfg.geocode_valid_band,
        soft_reject=cfg.geocode_soft_reject,
        soft_reject_min=cfg.geocode_soft_reject_min,
    )
    query = "Av. Caseros 1800, Ciudad Autónoma de Buenos Aires, C1264, Argentina"
    r = g.geocode(query, origin=origin)
    g.close()
    assert r.status in (STATUS_MATCHED, STATUS_LOW)
    # Truth OA ~1800; pin debe caer en CABA (1799/1801), no La Matanza.
    truth_lat, truth_lon = -34.6372, -58.3918
    err = haversine_km(r.lat, r.lon, truth_lat, truth_lon) * 1000
    assert err < 650, f"pin a {err:.0f} m del truth CABA"
    assert haversine_km(origin[0], origin[1], r.lat, r.lon) < 15


@pytest.mark.parametrize("case_id", ["santa_fe_2500", "caseros_1800", "corrientes_1234"])
def test_trap_cases_via_runner(case_id, homonym_index, tmp_path):
    data = _load_traps()
    case = next(c for c in data["cases"] if c["id"] == case_id)
    depot = DepotContext(
        lat=data["depot"]["lat"],
        lon=data["depot"]["lon"],
        city=data["depot"].get("city"),
        country=data["depot"].get("country"),
        address=data["depot"].get("address"),
        max_distance_km=500.0,
    )
    src = tmp_path / "in.csv"
    src.write_text(f"address\n{case['address']}\n", encoding="utf-8")
    out = tmp_path / "out.csv"
    geocode_run(
        src, out, homonym_index,
        depot=depot,
        cache_path=tmp_path / "cache.sqlite",
    )
    import csv
    row = next(csv.DictReader(out.open(encoding="utf-8")))
    truth = case["truth"]
    assert row.get("lat") and row.get("lng"), case_id
    err_m = haversine_km(
        float(row["lat"]), float(row["lng"]),
        truth["lat"], truth["lng"]) * 1000
    assert err_m <= case["max_error_m"], f"{case_id}: {err_m:.0f} m"
    depot_km = haversine_km(
        depot.lat, depot.lon, float(row["lat"]), float(row["lng"]))
    assert depot_km <= case["max_depot_km"]


def test_apply_guards_rechaza_matched_lejos_del_depot():
    from smart_import.geocoding.base import GeocodeResult
    from smart_import.geocoding.runner import GeocodeReport, _apply_depot_guards

    cfg = Config.from_env().replace(max_low_confidence_km=10.0)
    depot = DepotContext(lat=-34.598, lon=-58.416, max_distance_km=500.0)
    # Martínez ~13.8 km — debe caer con umbral 10 km aunque locality ya lo evite antes
    far = GeocodeResult(
        status=STATUS_MATCHED, lat=-34.4938614, lon=-58.4980751,
        confidence=1.0, precision="housenumber", source="osm",
    )
    report = GeocodeReport()
    out = _apply_depot_guards(far, depot=depot, cfg=cfg, report=report)
    assert out.status == STATUS_NOT_FOUND
    assert out.detail["reject_reason"] == "matched_far_from_depot"
    assert report.rejected_far == 1
