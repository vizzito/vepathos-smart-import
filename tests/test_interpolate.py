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


# ---------- calles homonimas: interpolar dentro de un tramo ----------

def _homonym_index(path):
    """'Condarco' en CABA (con addr:city) y en Lanus a 8 km (sin ciudad), como el GBA real."""
    import sqlite3

    from smart_import.geocoding.osm_index import SCHEMA_SQL

    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    rows, oid = [], 1
    # CABA: 401..699 impares cada 100, sobre lat fija
    for i, number in enumerate((401, 501, 599, 699)):
        rows.append((oid, -34.6260, -58.4680 - i * 0.0010, str(number),
                     "Ciudad Autónoma de Buenos Aires"))
        oid += 1
    # Lanus: 499, 549, 551, 599 — el 549 queda "mas cerca" en numeracion del 525
    for i, number in enumerate((499, 549, 551, 599)):
        rows.append((oid, -34.7055, -58.3270 - i * 0.0003, str(number), None))
        oid += 1
    for oid, lat, lon, num, city in rows:
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, kind, name, house_number,"
            " street, city, district, state, postcode, country, normalized_text)"
            " VALUES ('node',?,?,?,'building',NULL,?,'Condarco',?,NULL,NULL,NULL,'AR',?)",
            (oid, lat, lon, num, city, f"{num} condarco {(city or '').lower()}".strip()))
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) SELECT id, normalized_text FROM places")
    conn.execute("INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon)"
                 " SELECT id, lat, lat, lon, lon FROM places")
    conn.commit()
    conn.close()


def test_no_interpola_entre_dos_calles_homonimas(tmp_path):
    """'CONDARCO 525, CABA': el 501 de CABA y el 549 de Lanus quedaban como anclas y
    el pin caia entre los dos pueblos, a 7,9 km de la puerta."""
    from smart_import.geocoding.address import parse
    from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder

    path = tmp_path / "condarco.sqlite"
    _homonym_index(path)
    g = LocalOSMGeocoder(path)
    try:
        cand = g._interpolated_candidate(parse("Condarco 525, CABA, Argentina"), None)
    finally:
        g.close()
    assert cand is not None
    # sobre la calle de CABA (lat -34.626), entre el 501 y el 599
    assert cand.lat == pytest.approx(-34.6260, abs=0.0005)
    assert -58.4700 <= cand.lon <= -58.4690


def test_una_sola_calle_interpola_igual_que_antes(tmp_path):
    from smart_import.geocoding.address import parse
    from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder

    path = tmp_path / "condarco.sqlite"
    _homonym_index(path)
    g = LocalOSMGeocoder(path)
    try:
        # 650 solo tiene anclas en CABA (599 y 699): mismo punto que el lerp directo
        cand = g._interpolated_candidate(parse("Condarco 650, CABA"), None)
    finally:
        g.close()
    expected = interpolate_house(650, [HousePoint(599, -34.6260, -58.4700),
                                       HousePoint(699, -34.6260, -58.4710)])
    assert (cand.lat, cand.lon) == pytest.approx(expected, abs=1e-9)


def test_homonimas_sin_localidad_que_elija_no_interpola(tmp_path):
    """Sin CABA en la consulta no hay con que elegir tramo: mejor sin interpolar que un pin
    en el pueblo equivocado (Hector Barrueto, Santiago: 19 km entre tramos)."""
    from smart_import.geocoding.address import parse
    from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder

    path = tmp_path / "condarco.sqlite"
    _homonym_index(path)
    g = LocalOSMGeocoder(path)
    try:
        assert g._interpolated_candidate(parse("Condarco 525"), None) is None
    finally:
        g.close()


def test_avenida_con_alturas_salteadas_sigue_interpolando(tmp_path):
    """Anclas a 1,5 km con 210 numeros de diferencia (Via Emilia Ponente, Bolonia): es la
    misma avenida con huecos en OSM, no dos calles. La cuenta no cambia."""
    import sqlite3

    from smart_import.geocoding.address import parse
    from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder
    from smart_import.geocoding.osm_index import SCHEMA_SQL

    path = tmp_path / "emilia.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    for oid, (num, lon) in enumerate(((131, 11.3000), (341, 11.3190)), start=1):
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, kind, name, house_number,"
            " street, city, district, state, postcode, country, normalized_text)"
            " VALUES ('node',?,44.5076,?,'building',NULL,?,'Via Emilia Ponente',NULL,NULL,NULL,NULL,'IT',?)",
            (oid, lon, str(num), f"{num} via emilia ponente"))
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) SELECT id, normalized_text FROM places")
    conn.execute("INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon)"
                 " SELECT id, lat, lat, lon, lon FROM places")
    conn.commit()
    conn.close()
    g = LocalOSMGeocoder(path)
    try:
        cand = g._interpolated_candidate(parse("Via Emilia Ponente 157, Bologna"), None)
    finally:
        g.close()
    expected = interpolate_house(157, [HousePoint(131, 44.5076, 11.3000), HousePoint(341, 44.5076, 11.3190)])
    assert cand is not None and (cand.lat, cand.lon) == pytest.approx(expected, abs=1e-9)
