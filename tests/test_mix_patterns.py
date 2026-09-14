"""Patrones del corpus mix que el suite anterior no cubría.

Japón 0% no es el mix ni un depot equivocado: hay que identificar CJK y, si el
índice tiene el chome, encontrarlo. Chile 2000 km es OSM sin addr:city + homónimo
en la capital. Colombia CR/CL y Uruguay BIS son el parser comiéndose la vía.
"""
from __future__ import annotations

import sqlite3

import pytest

from smart_import.addresses import HeuristicAddressParser
from smart_import.geocoding.address import normalize_text, parse
from smart_import.geocoding.base import STATUS_NOT_FOUND
from smart_import.geocoding.depot_context import DepotContext
from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder
from smart_import.geocoding.osm_index import SCHEMA_SQL
from smart_import.geocoding.query import build_geocode_query
from smart_import.geocoding.scoring import (
    Candidate, _comma_place_labels, _query_place_labels, place_conflict,
)
from smart_import.resources import fold
from smart_import.text_script import cjk_tokens, has_cjk

pytestmark = pytest.mark.geocoding

# Centroides mínimos: los tests no dependen de cities15000.txt.
_FAKE_CENTROIDS = {
    "punta arenas": (-53.15, -70.91),
    "fredericia": (55.565, 9.753),
    "copenhagen": (55.676, 12.568),
    "radom": (51.403, 21.147),
    "warszawa": (52.230, 21.011),
    "warsaw": (52.230, 21.011),
    "sinaia": (45.350, 25.551),
    "bucharest": (44.426, 26.103),
    "bucuresti": (44.426, 26.103),
    "toronto": (43.653, -79.383),
    "quilicura": (-33.361, -70.729),
    "santiago": (-33.449, -70.669),
}


@pytest.fixture
def fake_city_centroids(monkeypatch):
    def lookup(city, country=None):
        key = fold(city or "")
        if key in _FAKE_CENTROIDS:
            return _FAKE_CENTROIDS[key]
        return None

    monkeypatch.setattr(
        "smart_import.geocoding.city_lookup.lookup_city_centroid", lookup)
    return lookup


def _index(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    for i, (lat, lon, house, street, city, text) in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, kind, name, house_number,"
            " street, city, district, state, postcode, country, normalized_text)"
            " VALUES ('node',?,?,?,?,NULL,?,?,?,NULL,NULL,NULL,NULL,?)",
            (i, lat, lon, "building", house, street, city, normalize_text(text)),
        )
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) "
                 "SELECT id, normalized_text FROM places")
    conn.execute("INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon) "
                 "SELECT id, lat, lat, lon, lon FROM places")
    conn.commit()
    conn.close()
    return LocalOSMGeocoder(path)


def _cand(*, house, street, city, text, lat=-33.45, lon=-70.66, cid=1):
    return Candidate(
        cid, lat, lon, "building", None, house, street,
        city, None, None, None, None, normalize_text(text),
    )


# ---------- CJK / kanji ----------

@pytest.mark.parametrize("text,expect", [
    ("京浜島二丁目, 11-9, 大田区", True),
    ("西大泉一丁目, 26-9, 練馬区", True),
    ("東京", True),
    ("CIRCUNVALACION, 1133, PUNTA ARENAS", False),
    ("CR 55, 93F-07", False),
    ("30a Street, 21039", False),
])
def test_has_cjk_detecta_kanji_no_latino(text, expect):
    assert has_cjk(text) is expect


def test_cjk_tokens_separa_chome_y_ward():
    assert cjk_tokens("京浜島二丁目, 11-9, 大田区") == ["京浜島二丁目", "大田区"]


def test_parser_jp_oa_chome_altura_ward():
    parsed = HeuristicAddressParser().parse("京浜島二丁目, 11-9, 大田区")
    assert parsed.get("road") == "京浜島二丁目"
    assert parsed.get("house_number") == "11-9"
    assert parsed.get("city") == "大田区"
    geo = parse("京浜島二丁目, 11-9, 大田区")
    assert geo.road == "京浜島二丁目"
    assert geo.house_number == "11-9"
    assert "大田区" in geo.normalized


def test_11_9_no_es_una_ciudad():
    assert "11 9" not in _comma_place_labels("京浜島二丁目, 11-9, 大田区")
    labels = _query_place_labels("京浜島二丁目, 11-9, 大田区")
    assert "大田区" in labels


def test_enhance_jp_conserva_el_ward():
    depot = DepotContext(city="Tokyo", country="Japan")
    row = {"city": "大田区", "region": "東京都", "country": "JP"}
    sent = build_geocode_query(
        "京浜島二丁目, 11-9, 大田区", row=row, depot=depot, enhance=True)
    assert "京浜島二丁目" in sent
    assert "11-9" in sent
    assert "大田区" in sent


def test_fts_jp_busca_chome_y_ward():
    g = LocalOSMGeocoder.__new__(LocalOSMGeocoder)
    p = parse("京浜島二丁目, 11-9, 大田区")
    words = g._search_words(p)
    assert "京浜島二丁目" in words
    assert "大田区" in words
    q = g._precise_query(p)
    assert q is not None
    assert "京浜島二丁目" in q
    assert "11" in q and "9" in q


def test_indice_con_kanji_resuelve_chome(tmp_path):
    g = _index(tmp_path / "jp.sqlite", [
        (35.570369, 139.762692, "11-9", "京浜島二丁目", "大田区",
         "11-9 京浜島二丁目 大田区 東京都"),
        (35.68, 139.76, "11-9", "Ginza", "Chuo",
         "11-9 ginza chuo tokyo"),
    ])
    r = g.geocode("京浜島二丁目, 11-9, 大田区", origin=(35.6762, 139.6503))
    assert r.has_coords
    assert r.lat == pytest.approx(35.570369, abs=1e-4)
    assert r.precision == "housenumber"


# ---------- parser: vias numeradas LatAm / plot EN / BIS ----------

@pytest.mark.parametrize("texto,road,house", [
    ("CR 55, 93F-07", "CR 55", "93F-07"),
    ("CL 3 SUR, 50F-39", "CL 3 SUR", "50F-39"),
    ("CL 70B, 27-87", "CL 70B", "27-87"),
    ("CR 24BC, 69D-174", "CR 24BC", "69D-174"),
    ("CL 101AC, 25-16", "CL 101AC", "25-16"),
    ("Carrera 7 # 45, Bogota", "Carrera 7", "45"),
    ("Calle 50 nro 1234, Medellin", "Calle 50", "1234"),
    ("LANCASTER, 3922BIS", "LANCASTER", "3922BIS"),
    ("CNO GIGANTES, 3493", "CNO GIGANTES", "3493"),
    ("30a Street, 21039 84888", "30a Street", "21039"),
    ("12 Street, 39942 91302", "12 Street", "39942"),
    ("VIALE DODICI GIUGNO, 15/B, Galvani, 40124", "VIALE DODICI GIUGNO", "15/B"),
    ("VIA SANTO STEFANO, 33/A, Galvani, 40125", "VIA SANTO STEFANO", "33/A"),
    ("VIA DE' TOSCHI, 2/F, Galvani, 40124", "VIA DE' TOSCHI", "2/F"),
])
def test_parser_patrones_mix(texto, road, house):
    parsed = HeuristicAddressParser().parse(texto)
    assert parsed.get("road") == road, parsed.components
    assert parsed.get("house_number") == house, parsed.components
    geo = parse(texto)
    assert geo.road == road
    assert geo.house_number == house


def test_cr_no_es_rua():
    """'r' de rua no puede comerse la R de CR 55."""
    parsed = HeuristicAddressParser().parse("CR 55, 93F-07")
    assert parsed.get("road") != "R 55"
    assert "CR" in (parsed.get("road") or "")


def test_ordinal_ingles_no_es_altura_bis():
    """'3922BIS' es altura; '7th Floor' no puede colarse como 7th."""
    parser = HeuristicAddressParser()
    assert parser.parse("LANCASTER, 3922BIS").get("house_number") == "3922BIS"
    india = parser.parse("7th Floor, Cyber Towers, Hitech City, Hyderabad 500081")
    assert india.get("house_number") != "7th"
    assert india.get("postcode") == "500081"


def test_esponente_slash_letra_no_es_el_cp():
    """15/B es altura+interno; 40124 es CP. Galvani no se pega a la calle."""
    from smart_import.geocoding.depot_context import DepotContext
    from smart_import.geocoding.query import build_geocode_query
    from smart_import.geocoding.scoring import Candidate, _house_number_match, score

    texto = "VIALE DODICI GIUGNO, 15/B, Galvani, 40124"
    parsed = HeuristicAddressParser().parse(texto)
    assert parsed.get("road") == "VIALE DODICI GIUGNO"
    assert parsed.get("house_number") == "15/B"
    assert parsed.get("postcode") == "40124"
    geo = parse(texto)
    assert geo.road == "VIALE DODICI GIUGNO"
    assert geo.house_number == "15/B"
    osm15 = Candidate(
        1, 44.48585, 11.34653, "building", None, "15", "Viale Dodici Giugno",
        "Bologna", None, None, "40124", "IT", "15 viale dodici giugno bologna",
    )
    osm15b = Candidate(
        2, 44.48585, 11.34653, "building", None, "15B", "Viale Dodici Giugno",
        "Bologna", None, None, "40124", "IT", "15b viale dodici giugno bologna",
    )
    assert _house_number_match(geo, osm15) == 1.0
    assert _house_number_match(geo, osm15b) == 1.0
    osm15a = Candidate(
        3, 44.48585, 11.34653, "building", None, "15/A", "Viale Dodici Giugno",
        "Bologna", None, None, "40124", "IT", "15a viale dodici giugno bologna",
    )
    assert _house_number_match(geo, osm15a) == 0.5
    _, parts = score(geo, osm15b)
    assert parts["house_number"] == 1.0

    sent = build_geocode_query(
        texto,
        row={"city": "Galvani", "postcode": "40124", "country": "IT"},
        depot=DepotContext(city="Bologna", country="Italy"),
        enhance=True,
    )
    assert "15/B" in sent
    assert sent.split(",")[0].count("40124") == 0 or "15/B" in sent.split(",")[0]
    core = sent.split(",")[0]
    assert "Galvani" not in core
    assert "40124" not in core


def test_fts_esponente_busca_15_y_15b_no_exige_b():
    g = LocalOSMGeocoder.__new__(LocalOSMGeocoder)
    p = parse("VIALE DODICI GIUGNO, 15/B, Galvani, 40124")
    clause = g._house_fts_clause(p.house_number or "15/B")
    assert clause is not None
    assert '"15"' in clause
    assert '"15B"' in clause or '"15b"' in clause.lower()
    assert " AND " not in clause
    q = g._precise_query(p)
    assert q is not None
    assert "40124" not in q or '"15"' in q
    words = g._search_words(p)
    folded = [w.casefold() for w in words]
    assert "galvani" not in folded
    assert "b" not in folded


def test_esponente_elige_15_en_el_indice(tmp_path):
    g = _index(tmp_path / "it.sqlite", [
        (44.48763, 11.34454, "1", "Viale Dodici Giugno", "Bologna",
         "1 viale dodici giugno bologna"),
        (44.48585, 11.34653, "15", "Viale Dodici Giugno", "Bologna",
         "15 viale dodici giugno bologna 40124"),
        (44.49, 11.35, "15", "Viale San Stefano", "Bologna",
         "15 viale san stefano bologna"),
    ])
    r = g.geocode(
        "VIALE DODICI GIUGNO, 15/B, 40124",
        origin=(44.49, 11.34),
    )
    assert r.has_coords
    assert r.lat == pytest.approx(44.48585, abs=1e-4)


# ---------- place_conflict: destino vs pin, no OSM vacío ----------

def test_ciudad_nombrada_veta_candidato_sin_city(fake_city_centroids):
    parsed = parse("CIRCUNVALACION, 1133, PUNTA ARENAS")
    santiago_vacio = _cand(
        house="1133", street="Circunvalacion", city=None,
        text="1133 circunvalacion",
    )
    punta = _cand(
        house="1133", street="Circunvalacion", city="Punta Arenas",
        text="1133 circunvalacion punta arenas",
        lat=-53.15, lon=-70.94, cid=2,
    )
    punta_sin_tag = _cand(
        house="1133", street="Circunvalacion", city=None,
        text="1133 circunvalacion punta arenas",
        lat=-53.15, lon=-70.94, cid=3,
    )
    assert place_conflict(parsed, santiago_vacio) is True
    assert place_conflict(parsed, punta) is False
    assert place_conflict(parsed, punta_sin_tag) is False


def test_caba_sin_addr_city_sigue_si_el_texto_es_caba():
    parsed = parse("AV CORRIENTES 919, CABA, Argentina")
    osm_sin_city = _cand(
        house="919", street="Avenida Corrientes", city=None,
        text="919 avenida corrientes ciudad autonoma de buenos aires",
        lat=-34.6034, lon=-58.3796,
    )
    assert place_conflict(parsed, osm_sin_city) is False


def test_olazabal_sin_ciudad_en_la_query_no_veta():
    """Sin ciudad en el address, OSM vacío no es conflicto (el enrich pone CABA)."""
    parsed = parse("OLAZABAL, 1359")
    assert not _query_place_labels(parsed.original)
    osm = _cand(
        house="1359", street="Olazabal", city=None,
        text="1359 olazabal",
        lat=-34.56, lon=-58.45,
    )
    assert place_conflict(parsed, osm) is False


def test_fredericia_veta_copenhague_aunque_osm_no_tenga_city(fake_city_centroids):
    parsed = parse("Dronningensgade, 20, Fredericia, 7000")
    capital_vacio = _cand(
        house="20", street="Dronningensgade", city=None,
        text="20 dronningensgade",
        lat=55.67, lon=12.59,
    )
    assert place_conflict(parsed, capital_vacio) is True


def test_former_toronto_no_veta_osm_sin_city(fake_city_centroids):
    """OA escribe 'former Toronto'; OSM Toronto suele omitir addr:city."""
    parsed = parse("Dupont St, 136, former Toronto")
    osm = _cand(
        house="136", street="Dupont Street", city=None,
        text="136 dupont street",
        lat=43.676, lon=-79.402,
    )
    assert place_conflict(parsed, osm) is False


def test_calle_con_nombre_de_capital_no_es_la_capital(fake_city_centroids):
    """'Calea Bucuresti, Sinaia' no es un pin en Bucarest."""
    parsed = parse("Calea Bucuresti, 30, SINAIA")
    capital = _cand(
        house="30", street="Calea Bucuresti", city="Bucharest",
        text="30 calea bucuresti bucharest",
        lat=44.43, lon=26.10,
    )
    assert place_conflict(parsed, capital) is True


def test_region_capital_inyectada_no_salva_otro_pueblo(fake_city_centroids):
    """Radom + Warszawa (voivodato) no autoriza un pin en Varsovia."""
    parsed = parse("Sadowa, 3, Radom, 26-604, Warszawa, Poland")
    warsaw = _cand(
        house="3", street="Sadowa", city="Warsaw",
        text="3 sadowa warsaw",
        lat=52.172, lon=21.229,
    )
    assert place_conflict(parsed, warsaw) is True


def test_comuna_del_extracto_no_veta_santiago_vacio(fake_city_centroids):
    parsed = parse("ISMAEL BRICEÑO, 1412, QUILICURA")
    osm = _cand(
        house="1412", street="Ismael Briceno", city=None,
        text="1412 ismael briceno",
        lat=-33.373, lon=-70.729,
    )
    assert place_conflict(parsed, osm) is False


def test_homonimo_sin_city_no_publica_pin_de_la_capital(tmp_path, fake_city_centroids):
    g = _index(tmp_path / "cl.sqlite", [
        (-33.45, -70.66, "1133", "Circunvalacion", None,
         "1133 circunvalacion"),
        (-33.44, -70.65, "670", "Club Hipico", None,
         "670 club hipico"),
    ])
    r = g.geocode(
        "CIRCUNVALACION, 1133, PUNTA ARENAS",
        origin=(-33.45, -70.66),
    )
    assert r.status == STATUS_NOT_FOUND
    assert r.detail.get("reason") == "place_mismatch"


def test_homonimo_con_city_en_el_texto_si_matchea(tmp_path, fake_city_centroids):
    g = _index(tmp_path / "cl2.sqlite", [
        (-53.154, -70.943, "1133", "Circunvalacion", None,
         "1133 circunvalacion punta arenas"),
        (-33.45, -70.66, "1133", "Circunvalacion", None,
         "1133 circunvalacion"),
    ])
    r = g.geocode(
        "CIRCUNVALACION, 1133, PUNTA ARENAS",
        origin=(-33.45, -70.66),
    )
    assert r.has_coords
    assert r.lat == pytest.approx(-53.154, abs=1e-3)
