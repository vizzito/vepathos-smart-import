"""lat/lng vs bbox de país: swap (Spain océano) vs punto real fuera de la capital."""
from smart_import.geocoding.coord_check import (
    apply_country_coord_fix,
    audit_truth_coords,
    classify_latlng,
)
from smart_import.tools.address_corpus import oa_row_to_record, _normalize_oa_row


def test_galicia_swap_es_oceano_indico():
    assert classify_latlng(-8.6986863, 43.2068282, "ES") == "swapped"
    lat, lon, fix = apply_country_coord_fix(-8.6986863, 43.2068282, "spain")
    assert fix == "swapped_lat_lng"
    assert classify_latlng(lat, lon, "ES") == "ok"


def test_prague_and_huara_no_son_swap():
    assert classify_latlng(50.0698786, 14.4096143, "CZ") == "ok"
    assert classify_latlng(-19.99817, -69.77112, "CL") == "ok"
    assert classify_latlng(-34.6037, -58.3816, "AR") == "ok"


def test_oa_row_corrige_swap_al_generar():
    row = _normalize_oa_row({
        "street": "LG OUTEIRO", "number": "S/N",
        "lat": "-8.6986863", "lon": "43.2068282",
    })
    rec = oa_row_to_record(row, source="oa", style="oa_default", country="ES")
    assert rec.extra.get("coord_fix") == "swapped_lat_lng"
    assert rec.lat == 43.2068282
    assert rec.lng == -8.6986863


def test_mix_skips_swapped_slice_before_osmium():
    filas = [
        {"address": "LG OUTEIRO", "lat": -8.70, "lng": 43.21, "country": "ES"}
        for _ in range(10)
    ]
    columnas = {"address": "address", "lat": "lat", "lng": "lng"}
    audit = audit_truth_coords(filas, columnas, "spain ES España")
    assert audit.swapped == 10
    assert audit.skip_reason and "invertidos" in audit.skip_reason


def test_null_island_no_elige_indice():
    from smart_import.geocoding.accuracy import data_bbox
    from smart_import.geocoding.coord_check import usable_truth_coord

    assert not usable_truth_coord(0.0, 0.0)
    assert usable_truth_coord(43.2, -8.7)
    filas = [
        {"lat": 43.2, "lng": -8.7, "address": "ok"},
        {"lat": 0.0, "lng": 0.0, "address": "street, number, postcode"},
    ]
    columnas = {"address": "address", "lat": "lat", "lng": "lng"}
    north, south, east, west = data_bbox(filas, columnas)
    assert south > 40
    assert north < 44


def test_chile_countrywide_no_skip_por_huara():
    filas = [
        {"address": "Santiago", "lat": -33.45, "lng": -70.67},
        {"address": "Huara", "lat": -19.99817, "lng": -69.77112},
    ]
    columnas = {"address": "address", "lat": "lat", "lng": "lng"}
    audit = audit_truth_coords(filas, columnas, "chile CL")
    assert audit.skip_reason is None
    assert audit.ok == 2
