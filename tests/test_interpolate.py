"""Interpolación de altura: anclas, paridad, polilínea del way."""
import pytest

from smart_import.geocoding.interpolate import (
    HousePoint,
    encode_polyline,
    interpolate_house,
    parse_polyline,
    pick_brackets,
    point_at_distance,
    project_onto_polyline,
)


def test_lerp_entre_dos_casas():
    lo = HousePoint(100, -34.60, -58.40)
    hi = HousePoint(200, -34.60, -58.38)
    lat, lon = interpolate_house(150, [lo, hi])
    assert lat == pytest.approx(-34.60)
    assert lon == pytest.approx(-58.39)


def test_prefiere_misma_paridad():
    """150 es par: 101 no cuenta; 100 y 200 sí."""
    houses = [
        HousePoint(100, 0.0, 0.0),
        HousePoint(101, 1.0, 1.0),
        HousePoint(200, 0.0, 10.0),
    ]
    pair = pick_brackets(150, houses, same_parity=True)
    assert pair is not None
    assert pair[0].number == 100 and pair[1].number == 200
    lat, lon = interpolate_house(150, houses)
    assert lat == pytest.approx(0.0)
    assert lon == pytest.approx(5.0)


def test_sin_paridad_cae_a_cualquier_lado():
    houses = [
        HousePoint(1502, -37.3226, -59.1618),
        HousePoint(1599, -37.3217, -59.1622),
    ]
    pt = interpolate_house(1572, houses)
    assert pt is not None
    lat, lon = pt
    assert min(-37.3226, -37.3217) < lat < max(-37.3226, -37.3217)
    assert min(-59.1618, -59.1622) <= lon <= max(-59.1618, -59.1622)


def test_no_interpola_si_el_tramo_es_enorme():
    houses = [
        HousePoint(100, -34.60, -58.40),
        HousePoint(9000, -34.62, -58.30),
    ]
    assert interpolate_house(5000, houses) is None


def test_no_interpola_fuera_del_rango():
    houses = [
        HousePoint(100, 0.0, 0.0),
        HousePoint(120, 0.0, 1.0),
    ]
    assert interpolate_house(500, houses) is None


def test_polyline_u_no_corta_la_manzana():
    """La cuerda iría por el medio; el way da la vuelta."""
    poly = [
        (0.0, 0.0),
        (0.0, 0.01),
        (0.01, 0.01),
    ]
    lo = HousePoint(0, 0.0, 0.0)
    hi = HousePoint(100, 0.01, 0.01)
    lat, lon = interpolate_house(50, [lo, hi], poly)
    chord_lat, chord_lon = 0.005, 0.005
    assert abs(lat - chord_lat) > 0.001 or abs(lon - chord_lon) > 0.001
    along = project_onto_polyline(poly, lat, lon)
    assert along is not None and along[1] < 5.0


def test_encode_parse_roundtrip():
    raw = encode_polyline([(1.5, 2.5), (1.6, 2.6)])
    assert parse_polyline(raw) == [(1.5, 2.5), (1.6, 2.6)]


def test_point_at_distance_extremos():
    poly = [(0.0, 0.0), (0.0, 0.01)]
    assert point_at_distance(poly, 0) == pytest.approx((0.0, 0.0), abs=1e-9)
    end = point_at_distance(poly, 1e9)
    assert end[1] == pytest.approx(0.01, abs=1e-6)
