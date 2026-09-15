"""Rescates del geocoder cuando la busqueda normal no da pin (2026-09-15).

Casos de dos planillas reales de Tandil donde 35 de 59 filas quedaban sin pin:
apellido solo, calle truncada, iniciales, typo fonetico, numero pegado, nombre
adelante y esquinas. Cada test fija tambien la guarda que evita el falso
positivo: unicidad, nunca pisar una calle existente, 'Fragata Sarmiento'.

Indice sintetico: una grilla chica con calles reales de Tandil. Coordenadas
inventadas pero coherentes (las alturas avanzan sobre la calle y los cruces
comparten punto), que es lo que necesitan interpolacion y esquinas.
"""
from __future__ import annotations

import csv
import sqlite3

import pytest

from smart_import.config import Config
from smart_import.geocoding.address import normalize_text
from smart_import.geocoding.base import STATUS_LOW, STATUS_NOT_FOUND
from smart_import.geocoding.depot_context import DepotContext
from smart_import.geocoding.intersection import intersection_candidates
from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder
from smart_import.geocoding.osm_index import SCHEMA_SQL
from smart_import.geocoding.runner import run as geocode_run
from smart_import.geocoding.street_resolver import StreetVocabulary, phonetic_key

pytestmark = pytest.mark.geocoding

ORIGIN = (-37.3200, -59.1300)
#: calles "horizontales" (lat fija) y "verticales" (lon fija). Se cruzan en la grilla.
H_STREETS = {
    "General Rudecindo Alvarado": -37.3100,
    "Intendente Dufau": -37.3120,
    "Sarmiento": -37.3140,
    "Fragata Sarmiento": -37.3160,
    "Los Crisantemos": -37.3180,
    "Lisandro de la Torre": -37.3220,
    "Perito Moreno": -37.3240,
    "Carlos Moreno": -37.3260,
}
V_STREETS = {
    "Avenida Lunghi": -59.1400,
    "Julián Navarro": -59.1360,
    "Pellegrini": -59.1320,
    "Entre Ríos": -59.1280,
    "Trabajadores Municipales": -59.1240,
}
LON0, LAT0 = -59.1420, -37.3080
STEP = 0.00012          # ~11 m entre alturas de a 10


def _rows():
    rows = []
    oid = 1
    for street, lat in H_STREETS.items():
        for i, number in enumerate(range(100, 2000, 100)):
            lon = LON0 + i * 0.0009
            rows.append(("node", oid, lat, lon, "building", None, str(number), street,
                         "Tandil", f"{number} {normalize_text(street)} tandil"))
            oid += 1
        rows.append(("way", oid, lat, LON0 + 0.008, "highway", street, None, None,
                     None, f"{normalize_text(street)}"))
        oid += 1
    for street, lon in V_STREETS.items():
        for i, number in enumerate(range(100, 2000, 100)):
            lat = LAT0 - i * 0.0009
            rows.append(("node", oid, lat, lon, "building", None, str(number), street,
                         "Tandil", f"{number} {normalize_text(street)} tandil"))
            oid += 1
        # nodos sobre cada cruce: la calle vertical pasa por la latitud de cada horizontal
        for h_lat in H_STREETS.values():
            rows.append(("node", oid, h_lat, lon, "highway", street, None, None, None,
                         f"{normalize_text(street)}"))
            oid += 1
    return rows


@pytest.fixture(scope="module")
def index_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("rescue") / "tandil_grid.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    for osm_type, osm_id, lat, lon, kind, name, num, street, city, text in _rows():
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, kind, name, house_number,"
            " street, city, district, state, postcode, country, normalized_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,'AR',?)",
            (osm_type, osm_id, lat, lon, kind, name, num, street, city, text))
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) SELECT id, normalized_text FROM places")
    conn.execute("INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon)"
                 " SELECT id, lat, lat, lon, lon FROM places")
    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="module")
def vocab(index_path):
    conn = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    yield StreetVocabulary(conn)
    conn.close()


@pytest.fixture(scope="module")
def geocoder(index_path):
    cfg = Config.from_env()
    g = LocalOSMGeocoder(index_path, match_threshold=cfg.match_threshold,
                         low_threshold=cfg.low_confidence_threshold,
                         street_level_floor=cfg.geocode_street_level_floor,
                         street_match_min=cfg.geocode_street_match_min,
                         review_band=cfg.geocode_review_band,
                         valid_band=cfg.geocode_valid_band,
                         soft_reject=cfg.geocode_soft_reject,
                         soft_reject_min=cfg.geocode_soft_reject_min)
    yield g
    g.close()


# ---------- resolucion de nombres ----------

@pytest.mark.parametrize("query,expected,kind", [
    ("alvarado", "General Rudecindo Alvarado", "surname"),
    ("Dufau", "Intendente Dufau", "surname"),
    ("Trabajadores Mun", "Trabajadores Municipales", "truncated"),
    ("Lisandro dlt", "Lisandro de la Torre", "initials"),
    ("Lungui", "Avenida Lunghi", "typo"),
    ("Pelegrini", "Pellegrini", "typo"),
    ("los crisantelmos", "Los Crisantemos", "typo"),
    ("Mariela entre rios", "Entre Ríos", "prefix_noise"),
])
def test_resuelve_la_forma_en_que_la_gente_escribe_la_calle(vocab, query, expected, kind):
    res = vocab.resolve(query)
    assert res is not None, query
    assert (res.name, res.kind) == (expected, kind)


def test_no_pisa_una_calle_que_existe(vocab):
    """'Sarmiento' existe: jamas se convierte en 'Fragata Sarmiento'."""
    assert vocab.exists("Sarmiento")
    assert vocab.resolve("Sarmiento") is None


def test_dos_calles_igual_de_buenas_no_se_elige(vocab):
    """'moreno' explica 'Perito Moreno' y 'Carlos Moreno' por igual."""
    assert vocab.resolve("moreno") is None


@pytest.mark.parametrize("query", ["Juan", "Caja", "Total", "Pedido", "Gonzalo", "bon o bon"])
def test_palabras_sueltas_no_se_inventan_calle(vocab, query):
    assert vocab.resolve(query) is None


def test_clave_fonetica_junta_errores_de_oido_y_separa_nombres_distintos():
    assert phonetic_key("Lunghi") == phonetic_key("Lungui")
    assert phonetic_key("Pellegrini") == phonetic_key("Pelegrini")
    assert phonetic_key("Jazmines") == phonetic_key("jasmines")
    assert phonetic_key("Lima") != phonetic_key("Lina")


# ---------- esquinas ----------

@pytest.mark.parametrize("text,pair", [
    ("Garibaldi y Montiel", ("Garibaldi", "Montiel")),
    ("MAIPU Y MONTIEL, Tandil, Argentina", ("MAIPU", "MONTIEL")),
    ("Ferrari (entre rios y dufau)", ("entre rios", "dufau")),
    ("Colon esq. Arana", ("Colon", "Arana")),
    ("5th Ave & 42nd St", ("5th Ave", "42nd St")),
])
def test_separa_esquinas(text, pair):
    assert intersection_candidates(text)[0] == pair


def test_esquina_con_calles_resueltas_cae_en_el_cruce(geocoder):
    """'lungui' (typo) y 'alvarado' (apellido): las dos se resuelven y se cruzan."""
    r = geocoder.geocode("lungui y alvarado, Tandil, Argentina", origin=ORIGIN)
    assert r.precision == "intersection" and r.status == STATUS_LOW
    assert r.detail["soft_reject"] is True
    assert r.lat == pytest.approx(H_STREETS["General Rudecindo Alvarado"], abs=0.0002)
    assert r.lon == pytest.approx(V_STREETS["Avenida Lunghi"], abs=0.0002)


def test_esquina_que_no_existe_no_inventa_pin(geocoder):
    r = geocoder.geocode("Juan y Pedro, Tandil", origin=ORIGIN)
    assert not r.has_coords


# ---------- cascada del geocoder ----------

def test_apellido_solo_da_pin_ambar_sobre_la_calle_oficial(geocoder):
    """Puede resolverlo la busqueda normal (soft-reject) o el rescate; en los dos
    casos el pin queda sobre la calle oficial y en ambar."""
    r = geocoder.geocode("alvarado 500, Tandil, Argentina", origin=ORIGIN)
    assert r.has_coords and r.status == STATUS_LOW
    assert r.lat == pytest.approx(H_STREETS["General Rudecindo Alvarado"], abs=0.0002)
    assert r.detail["soft_reject"] is True               # corregir no pinta verde


def test_typo_sin_rescate_no_da_pin_y_con_rescate_si(geocoder):
    core = geocoder._geocode_core("Pelegrini 500, Tandil", origin=ORIGIN)
    r = geocoder.geocode("Pelegrini 500, Tandil", origin=ORIGIN)
    assert not geocoder._publishable(core)
    assert r.has_coords and r.detail["street_resolved"]["to"] == "Pellegrini"
    assert r.lon == pytest.approx(V_STREETS["Pellegrini"], abs=0.0002)


def test_numero_pegado_y_typo(geocoder):
    r = geocoder.geocode("Los crisantelmos1200, Tandil", origin=ORIGIN)
    assert r.has_coords
    assert r.detail["glued_number"]["to"].startswith("Los crisantelmos 1200")


def test_fragata_sarmiento_sigue_siendo_fragata(geocoder):
    r = geocoder.geocode("Fragata Sarmiento 500, Tandil", origin=ORIGIN)
    assert r.has_coords
    assert abs(r.lat - H_STREETS["Fragata Sarmiento"]) < abs(r.lat - H_STREETS["Sarmiento"])


def test_gate_pide_evidencia_al_indice(geocoder):
    assert geocoder.street_evidence("alvarado 471, Tandil") == "calle del indice (corregida)"
    assert geocoder.street_evidence("Sarmiento 245, Tandil") == "calle del indice"
    assert geocoder.street_evidence("lungui y alvarado") == "esquina de calles del indice"
    assert geocoder.street_evidence("Caja 12") is None
    assert geocoder.street_evidence("Gonzalo") is None


# ---------- runner: el camino del producto ----------

def _run(tmp_path, index_path, addresses):
    src, dst = tmp_path / "in.csv", tmp_path / "out.csv"
    with src.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["delivery_id", "address"])
        for i, a in enumerate(addresses):
            w.writerow([f"D{i}", a])
    depot = DepotContext(lat=ORIGIN[0], lon=ORIGIN[1], city="Tandil", country="Argentina",
                         max_distance_km=500.0)
    report = geocode_run(src, dst, index_path, origin=ORIGIN, config=Config.from_env(),
                         cache_path=tmp_path / "cache.sqlite", depot=depot)
    with dst.open(encoding="utf-8") as fh:
        return report, list(csv.DictReader(fh))


def test_runner_rescata_lo_que_el_gate_descartaba(tmp_path, index_path):
    report, rows = _run(tmp_path, index_path, [
        "alvarado 500",          # 3 digitos: antes 0.45 < 0.50, descartada sin buscar
        "lungui y alvarado",     # esquina sin altura
        "Gonzalo",               # un nombre: sigue afuera
        "Caja 12",               # palabra + numero que no es calle: sigue afuera
    ])
    by_addr = {r["address"]: r for r in rows}
    assert by_addr["alvarado 500"]["geocode_band"] == "review"
    assert by_addr["lungui y alvarado"]["geocode_precision"] == "intersection"
    assert by_addr["Gonzalo"]["geocode_band"] == "needs_geocoding"
    assert by_addr["Caja 12"]["geocode_band"] == "needs_geocoding"
    assert report.rescued_by_index == 2
    assert report.reasons["needs_geocoding:sin_evidencia"] == 2


def test_runner_no_descarta_la_esquina_que_el_geocoder_ya_valido(tmp_path, index_path):
    """'Garibaldi y Montiel' (Tandil, 2026-09-15): el gate la descartaba, el indice la
    rescataba y `geocode` encontraba el cruce, pero el re-chequeo del runner le
    buscaba una calle a la frase entera y la tiraba. Tambien desde la cache."""
    # la segunda fila identica sale de la cache, que no guardaba los cruces
    report, rows = _run(tmp_path, index_path, ["Pellegrini y Sarmiento"] * 2)
    for row in rows:
        assert row["geocode_precision"] == "intersection", row["geocode_reason"]
        assert row["geocode_band"] == "review"
        assert float(row["lat"]) == pytest.approx(H_STREETS["Sarmiento"], abs=0.0002)
        assert float(row["lng"]) == pytest.approx(V_STREETS["Pellegrini"], abs=0.0002)
    assert report.cache["hits"] == 1


def test_runner_no_descarta_el_rescate_que_el_geocoder_ya_valido(tmp_path, index_path):
    """'Los crisantelmos1904' (Tandil): el gate da 0 (numero pegado), el geocoder la
    rescata sobre 'Los Crisantemos', y el runner la re-validaba con la calle que
    sale del texto crudo ('los') y la tiraba."""
    _, rows = _run(tmp_path, index_path, ["Los crisantelmos1200"] * 2)
    for row in rows:
        assert row["geocode_band"] == "review", row["geocode_reason"]
        assert float(row["lat"]) == pytest.approx(H_STREETS["Los Crisantemos"], abs=0.0002)


def test_ambar_nunca_muestra_mas_que_el_piso_verde(tmp_path, index_path):
    """'Review 99%' al lado de 'Valid 98%' (Tandil): el % visible del ambar se
    recorta debajo del corte verde; el score real queda en geocode_raw_score."""
    cfg = Config.from_env()
    _, rows = _run(tmp_path, index_path, ["alvarado 500", "Sarmiento 450"])
    for row in rows:
        if row["geocode_band"] == "review":
            assert float(row["geocode_confidence"]) < cfg.geocode_valid_band
            assert row["geocode_raw_score"]


# ---------- guardas de plausibilidad de un rescate ----------

@pytest.fixture(scope="module")
def two_towns_index(tmp_path_factory):
    """'Condarco' en dos pueblos a 8 km, sin addr:city (como el GBA real)."""
    path = tmp_path_factory.mktemp("homonyms") / "two_towns.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    rows = []
    oid = 1
    for lat0 in (-34.600, -34.672):
        for i, number in enumerate(range(100, 1200, 100)):
            rows.append(("node", oid, lat0, -58.45 + i * 0.0009, "building", None, str(number),
                         "Condarco", None, f"{number} condarco"))
            oid += 1
    for i, number in enumerate(range(100, 1200, 100)):
        rows.append(("node", oid, -34.620, -58.40 + i * 0.0009, "building", None, str(number),
                     "Weißgasse", None, f"{number} weißgasse"))
        oid += 1
    for osm_type, osm_id, lat, lon, kind, name, num, street, city, text in rows:
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, kind, name, house_number,"
            " street, city, district, state, postcode, country, normalized_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?)",
            (osm_type, osm_id, lat, lon, kind, name, num, street, city, text))
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) SELECT id, normalized_text FROM places")
    conn.execute("INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon)"
                 " SELECT id, lat, lat, lon, lon FROM places")
    conn.commit()
    conn.close()
    return path


def test_calle_homonima_en_dos_pueblos_forma_dos_grupos(two_towns_index):
    g = LocalOSMGeocoder(two_towns_index)
    try:
        assert len(g._street_groups(("Condarco",))) == 2
        assert len(g._street_groups(("Weißgasse",))) == 1
    finally:
        g.close()


def test_rescate_sobre_calle_homonima_sin_localidad_se_descarta(two_towns_index):
    from smart_import.geocoding.base import GeocodeResult, STATUS_LOW

    g = LocalOSMGeocoder(two_towns_index)
    try:
        pin = GeocodeResult(status=STATUS_LOW, lat=-34.672, lon=-58.446, confidence=0.9,
                            precision="housenumber", matched_text="500 condarco")
        ok, why = g.rescue_plausible(pin, "CONDARCO 525, CABA, Argentina")
        assert not ok and "homonima" in why
    finally:
        g.close()


def test_rescate_a_nivel_calle_nunca_se_acepta(two_towns_index):
    from smart_import.geocoding.base import GeocodeResult, STATUS_LOW

    g = LocalOSMGeocoder(two_towns_index)
    try:
        pin = GeocodeResult(status=STATUS_LOW, lat=-34.620, lon=-58.40, confidence=0.9,
                            precision="street", matched_text="weißgasse",
                            detail={"reason": "street_level_match"})
        assert g.rescue_plausible(pin, "Weißgasse 29")[0] is False
    finally:
        g.close()


def test_sharp_s_y_ss_son_la_misma_calle(two_towns_index):
    conn = sqlite3.connect(f"file:{two_towns_index}?mode=ro", uri=True)
    try:
        assert StreetVocabulary(conn).exists("WEISSGASSE")
    finally:
        conn.close()


def test_una_localidad_cercana_no_se_resuelve_como_calle():
    from smart_import.geocoding.osm_geocoder import _is_nearby_locality

    assert _is_nearby_locality("Uccle", (50.85, 4.35))
    assert _is_nearby_locality("Palermo", (-34.6, -58.4))            # barrio curado
    assert not _is_nearby_locality("alvarado", (-37.35, -59.13))     # Alvarado MX, lejos
    assert not _is_nearby_locality("Lungui", (-37.35, -59.13))


@pytest.mark.parametrize("query,expected,kind", [
    ("Gral Rudecindo Alvarado", "General Rudecindo Alvarado", "abbreviation"),
    ("Lisandro de la T", "Lisandro de la Torre", "subsequence"),   # T = inicial de Torre
    ("Gral Moreno", None, None),        # no hay 'General Moreno'; 'Perito'/'Carlos' no se inventan
])
def test_titulos_abreviados_solo_en_la_resolucion(vocab, query, expected, kind):
    from smart_import.geocoding.address import normalize_text

    res = vocab.resolve(query)
    assert (res.name if res else None, res.kind if res else None) == (expected, kind)
    assert normalize_text("Gral Paz") == "gral paz"          # la normalizacion global no cambia


def test_orden_ingles_con_localidad_se_reordena_como_rescate():
    from smart_import.geocoding.osm_geocoder import _NUMBER_FIRST

    m = _NUMBER_FIRST.match("32 Flinders Street, Melbourne, VIC, Australia")
    assert m and m.group("num") == "32" and m.group("street") == "Flinders Street"
    assert _NUMBER_FIRST.match("Flinders Street 32, Melbourne") is None


def test_rescate_fuera_de_la_ciudad_que_nombra_la_consulta(geocoder):
    """'בני ברק' pedido y el pin en Tel Aviv: el radio sale de la poblacion de GeoNames."""
    from smart_import.geocoding.base import GeocodeResult, STATUS_LOW

    tandil = (-37.3287, -59.1369)
    inside = GeocodeResult(status=STATUS_LOW, lat=-37.305, lon=-59.17, confidence=0.9)
    far = GeocodeResult(status=STATUS_LOW, lat=-37.50, lon=-59.30, confidence=0.9)
    assert geocoder._outside_named_city(inside, "alvarado 471, Tandil, Argentina", tandil) is None
    assert (geocoder._outside_named_city(far, "alvarado 471, Tandil, Argentina", tandil) or "").lower() == "tandil"


def test_una_palabra_casi_igual_a_una_de_calle_no_es_ruido(vocab):
    """'Sladanha' es 'Saldanha' mal escrito: no se descarta como nombre de comercio."""
    assert vocab._near_street_word("sarmiemto")
    assert not vocab._near_street_word("kiosco")
    assert not vocab._near_street_word("sarmiento")      # igual: es la calle, no un typo
