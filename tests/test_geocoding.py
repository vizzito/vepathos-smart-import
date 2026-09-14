"""Geocoding: registry de PBF, normalizacion de direcciones, scoring y runner.

El indice se arma sinteticamente para que la suite no dependa de un .osm.pbf de
25 MB. Los tests contra un PBF real viven en test_geocoding_real.py y se saltan
si no hay extracts disponibles.

Correr solo geocoding:   pytest -q -m geocoding
PBF real (opt-in):       pytest -q -m real_geo
"""
import csv
import sqlite3

import pytest

pytestmark = pytest.mark.geocoding

from smart_import.geocoding.address import normalize_text, parse
from smart_import.geocoding.base import (
    STATUS_ALREADY, STATUS_LOW, STATUS_MATCHED, STATUS_NOT_FOUND,
)
from smart_import.geocoding.osm_index import SCHEMA_SQL
from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder
from smart_import.geocoding.pbf_registry import PbfRegistry, _parse
from pathlib import Path

PLACES = [
    # (osm_type, osm_id, lat, lon, kind, name, housenumber, street, city, postcode)
    ("node", 1, -34.6037, -58.3816, "building", None, "1234", "Avenida Corrientes",
     "Buenos Aires", "1043"),
    ("node", 2, -34.6040, -58.3820, "building", None, "1236", "Avenida Corrientes",
     "Buenos Aires", "1043"),
    ("way", 3, -34.6050, -58.3900, "highway", "Avenida Corrientes", None,
     "Avenida Corrientes", "Buenos Aires", None),
    ("node", 4, -31.4201, -64.1888, "building", None, "1234", "Avenida Corrientes",
     "Cordoba", "5000"),          # calle homonima en otra ciudad: la trampa clasica
    ("node", 5, 19.1197, 72.8464, "building", None, "14B", "Shanti Nagar",
     "Mumbai", "400069"),
    ("node", 6, -34.5898, -58.3772, "building", None, "900", "Avenida del Libertador",
     "Buenos Aires", "1001"),
]


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    path = tmp_path_factory.mktemp("idx") / "test.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    for osm_type, osm_id, lat, lon, kind, name, num, street, city, postcode in PLACES:
        searchable = " ".join(p for p in (num, street, name, city, postcode) if p)
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, kind, name, house_number,"
            " street, city, district, state, postcode, country, normalized_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,?,NULL,?)",
            (osm_type, osm_id, lat, lon, kind, name, num, street, city, postcode,
             normalize_text(searchable)))
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) "
                 "SELECT id, normalized_text FROM places")
    conn.execute("INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon) "
                 "SELECT id, lat, lat, lon, lon FROM places")
    conn.commit()
    conn.close()
    return path


# ---------- normalizacion de direcciones ----------

@pytest.mark.parametrize("address,house,postcode", [
    ("Av. Corrientes 1234, CABA, Argentina", "1234", None),
    ("900 Broadway, Seattle, WA 98122", "900", "98122"),
    ("Flat 14B, Shanti Nagar, Andheri East, Mumbai 400069", "14B", "400069"),
    ("Cerrito 2061, Buenos Aires", "2061", None),
])
def test_extrae_altura_y_codigo_postal(address, house, postcode):
    p = parse(address)
    assert p.house_number == house
    assert p.postcode == postcode


def test_parse_no_come_ordinal_us():
    """'NE 1st Ave 350' no puede resolver house=1 (el ordinal)."""
    p = parse("350 NE 1st Ave, Miami, United States")
    assert p.house_number == "350"
    assert "1st" in (p.road or "")
    p2 = parse("NE 1st Ave 350, Miami, United States")
    assert p2.house_number == "350"


def test_no_confunde_altura_con_codigo_postal():
    """'Av. Corrientes 1234' -> 1234 es la altura, no un CP de 4 digitos."""
    p = parse("Av. Corrientes 1234, CABA")
    assert p.house_number == "1234" and p.postcode is None


def test_direccion_india_sin_calle_ni_altura():
    p = parse("Flat 14B, Shanti Nagar, Near Hanuman Temple, Andheri East, Mumbai 400069")
    assert p.postcode == "400069"
    assert p.unit and "Flat" in p.unit
    assert p.landmark and "Hanuman" in p.landmark


def test_expande_abreviaturas():
    assert "avenida" in normalize_text("Av. Corrientes")
    assert "street" in normalize_text("900 Broadway St")


def test_conserva_el_original():
    p = parse("Av. Corrientes 1234, CABA")
    assert p.original == "Av. Corrientes 1234, CABA"
    assert p.normalized != p.original


# ---------- registry de PBF ----------

def test_parsea_bbox_del_nombre_del_extract():
    entry = _parse(Path("/data/_extracts/norway/n60.02_s59.55_e10.95_w10.68-pyrosm.osm.pbf"))
    assert entry.has_bbox
    assert (entry.north, entry.south, entry.east, entry.west) == (60.02, 59.55, 10.95, 10.68)
    assert entry.zone == "norway"
    assert entry.key == "n60.02_s59.55_e10.95_w10.68"


def test_pbf_de_pais_no_tiene_bbox_y_queda_como_cobertura_amplia():
    entry = _parse(Path("/data/south-america/argentina-pyrosm.osm.pbf"))
    assert not entry.has_bbox
    assert entry.area == float("inf")


def test_elige_el_extract_mas_chico_que_cubre_el_punto():
    grande = _parse(Path("/d/z/n61.00_s59.00_e12.00_w9.00-pyrosm.osm.pbf"))
    chico = _parse(Path("/d/z/n60.02_s59.55_e10.95_w10.68-pyrosm.osm.pbf"))
    registry = PbfRegistry([grande, chico])
    assert registry.find_for_point(59.91, 10.75) is chico


def test_sin_cobertura_devuelve_none():
    registry = PbfRegistry([_parse(Path("/d/z/n60.02_s59.55_e10.95_w10.68-pyrosm.osm.pbf"))])
    assert registry.find_for_point(-34.60, -58.38) is None


def test_fallback_a_pbf_de_pais_cuando_no_hay_extract():
    pais = _parse(Path("/data/south-america/argentina-pyrosm.osm.pbf"))
    otro = _parse(Path("/data/europe/norway-pyrosm.osm.pbf"))
    # size_bytes=0 en paths ficticios; da igual, solo hay un candidato que cubre BA
    registry = PbfRegistry([pais, otro])
    chosen = registry.resolve(lat=-34.60, lon=-58.38)
    assert chosen is pais
    assert chosen.country_slug == "argentina"


def test_caba_no_elige_uruguay_aunque_sea_mas_chico():
    """Regresión: bbox flojo de UY + 'más chico gana' geocodificaba BA en Uruguay."""
    from smart_import.geocoding.pbf_registry import PbfEntry

    ar = PbfEntry(
        path=Path("/data/south-america/argentina-pyrosm.osm.pbf"),
        zone="south-america", size_bytes=427_000_000,
    )
    uy = PbfEntry(
        path=Path("/data/south-america/uruguay-pyrosm.osm.pbf"),
        zone="south-america", size_bytes=56_000_000,
    )
    registry = PbfRegistry([ar, uy])
    chosen = registry.resolve(lat=-34.593, lon=-58.394)
    assert chosen is ar
    assert chosen.country_slug == "argentina"


def test_zone_hint_ar_no_elige_ashmore_cartier():
    """Regresión Tandil: zone_hint='AR' matcheaba 'ashmore-cARtier' por substring.

    El depot manda country=AR; el resolve viejo hacía `'ar' in filename` y
    elegía el PBF más chico (islas Ashmore y Cartier, Océano Índico). osmium
    cortaba 92 bytes y el geocode fallaba con 'extract inútil'.
    """
    from smart_import.geocoding.pbf_registry import PbfEntry

    ar = PbfEntry(
        path=Path("/data/south-america/argentina-pyrosm.osm.pbf"),
        zone="south-america", size_bytes=427_000_000,
    )
    ashmore = PbfEntry(
        path=Path("/data/australia/ashmore-cartier-pyrosm.osm.pbf"),
        zone="australia", size_bytes=120_000,
    )
    registry = PbfRegistry([ar, ashmore])
    tandil = (-37.3004059, -59.0876735)
    chosen = registry.resolve(lat=tandil[0], lon=tandil[1], zone_hint="AR")
    assert chosen is ar
    assert chosen.country_slug == "argentina"
    # Sin coordenadas, el ISO-2 expandido sigue apuntando a Argentina, no a ashmore.
    by_hint = registry.resolve(zone_hint="AR")
    assert by_hint is ar


def test_zone_hint_br_resuelve_brazil_no_substring():
    """BR → Brasil/brazil; no debe caer en un PBF chico por substring."""
    from smart_import.geocoding.pbf_registry import PbfEntry

    br = PbfEntry(
        path=Path("/data/south-america/brazil-pyrosm.osm.pbf"),
        zone="south-america", size_bytes=800_000_000,
    )
    tiny = PbfEntry(
        path=Path("/data/other/barbados-pyrosm.osm.pbf"),
        zone="other", size_bytes=50_000,
    )
    registry = PbfRegistry([br, tiny])
    assert registry.resolve(lat=-23.55, lon=-46.63, zone_hint="BR") is br
    assert registry.resolve(zone_hint="BR") is br


def test_zone_hint_florida_refina_sobre_usa():
    """Con coords en Miami, hint 'florida' debe preferir la subdivisión."""
    from smart_import.geocoding.pbf_registry import PbfEntry

    usa = PbfEntry(
        path=Path("/data/north-america/united-states-pyrosm.osm.pbf"),
        zone="north-america", size_bytes=9_000_000_000,
    )
    florida = PbfEntry(
        path=Path("/data/north-america/us_tile/florida-pyrosm.osm.pbf"),
        zone="us_tile", size_bytes=400_000_000,
    )
    registry = PbfRegistry([usa, florida])
    miami = (25.7617, -80.1918)
    # Sin hint gana el de mayor margen interior (florida suele ganar por bbox chico).
    chosen = registry.resolve(lat=miami[0], lon=miami[1], zone_hint="florida")
    assert chosen is florida


def test_extract_chico_gana_sobre_pais():
    extract = _parse(Path("/d/_extracts/ba/n-34.58_s-34.92_e-58.15_w-58.62-pyrosm.osm.pbf"))
    pais = _parse(Path("/data/south-america/argentina-pyrosm.osm.pbf"))
    registry = PbfRegistry([extract, pais])
    assert registry.resolve(lat=-34.60, lon=-58.38) is extract


def test_pbf_de_pais_cubre_puntos_fuera_de_latam():
    """Bounds viven en pbf_country_bounds.json: India / USA / Japón sin extract."""
    india = _parse(Path("/data/asia/india-pyrosm.osm.pbf"))
    usa = _parse(Path("/data/north-america/usa-pyrosm.osm.pbf"))
    japan = _parse(Path("/data/asia/japan-pyrosm.osm.pbf"))
    registry = PbfRegistry([india, usa, japan])
    assert registry.resolve(lat=19.076, lon=72.877) is india          # Mumbai
    assert registry.resolve(lat=25.7617, lon=-80.1918) is usa         # Miami
    assert registry.resolve(lat=35.6762, lon=139.6503) is japan       # Tokyo


def test_pbf_por_region_del_cutter_elige_la_subdivision():
    """El cutter parte US/India/Japón/UK: no hay usa.pbf, hay florida / kanto / …"""
    base = Path("/data")
    florida = _parse(base / "north-america_tile_x/us_tile_y/florida-pyrosm.osm.pbf")
    western = _parse(base / "asia_tile_x/india/western-zone-pyrosm.osm.pbf")
    kanto = _parse(base / "asia_tile_x/japan/kanto-pyrosm.osm.pbf")
    england = _parse(base / "europe_tile_x/united-kingdom/england-pyrosm.osm.pbf")
    us_ga = _parse(base / "north-america_tile_x/us_tile_y/georgia-pyrosm.osm.pbf")
    eu_ga = _parse(base / "europe_tile_x/georgia-pyrosm.osm.pbf")
    bahamas = _parse(base / "central-america_tile_x/bahamas-pyrosm.osm.pbf")
    registry = PbfRegistry([florida, western, kanto, england, us_ga, eu_ga, bahamas])
    assert registry.resolve(lat=25.7617, lon=-80.1918) is florida     # Miami, no Bahamas
    assert registry.resolve(lat=19.076, lon=72.877) is western        # Mumbai
    assert registry.resolve(lat=35.6762, lon=139.6503) is kanto       # Tokyo
    assert registry.resolve(lat=51.5074, lon=-0.1278) is england      # London
    assert registry.resolve(lat=33.7490, lon=-84.3880) is us_ga       # Atlanta
    assert registry.resolve(lat=41.7151, lon=44.8271) is eu_ga        # Tbilisi


def test_pbf_de_pais_sin_bounds_no_se_adivina():
    desconocido = _parse(Path("/data/other/atlantis-pyrosm.osm.pbf"))
    registry = PbfRegistry([desconocido])
    assert registry.resolve(lat=-34.60, lon=-58.38) is None


def test_registry_vacio_si_el_directorio_no_existe():
    assert PbfRegistry.scan("/no/existe/nada").entries == []


# ---------- geocoder ----------

def test_altura_exacta_da_match(index):
    r = LocalOSMGeocoder(index).geocode("Av. Corrientes 1234, Buenos Aires")
    assert r.status == STATUS_MATCHED
    assert r.precision == "housenumber"
    assert r.lat == pytest.approx(-34.6037, abs=1e-3)


def test_calle_sin_altura_exacta_es_confianza_baja_no_descarte(index):
    """Un match a nivel calle es util: se entrega con precision 'street' y marcado."""
    r = LocalOSMGeocoder(index).geocode("Av. Corrientes 9999, Buenos Aires")
    assert r.status == STATUS_LOW
    assert r.precision == "street"
    assert r.has_coords


def test_calle_sin_pedir_altura_nunca_es_valid(index):
    """OSM tiene 'Corrientes 1234'; la query no pidio puerta → street/review.

    Si heredamos precision=housenumber del candidato, CABA 2907 pinta verde
    un centroide a 500 m (Alfredo Colmo, Zuviria, …).
    """
    r = LocalOSMGeocoder(index).geocode("Av. Corrientes, Buenos Aires")
    assert r.has_coords
    assert r.precision == "street"
    assert r.status == STATUS_LOW


def test_direccion_inexistente_no_inventa_coordenadas(index):
    r = LocalOSMGeocoder(index).geocode("Calle Falsa 123, Springfield")
    assert r.status == STATUS_NOT_FOUND
    assert r.lat is None and r.lon is None


def test_el_depot_desempata_entre_calles_homonimas(index):
    """La misma calle existe en Buenos Aires y en Cordoba: gana la cercana al depot."""
    geocoder = LocalOSMGeocoder(index)
    porteno = geocoder.geocode("Av. Corrientes 1234", origin=(-34.60, -58.38))
    cordobes = geocoder.geocode("Av. Corrientes 1234", origin=(-31.42, -64.19))
    assert porteno.lat == pytest.approx(-34.6037, abs=1e-2)
    assert cordobes.lat == pytest.approx(-31.4201, abs=1e-2)


def test_el_bbox_acota_la_busqueda(index):
    r = LocalOSMGeocoder(index).geocode(
        "Av. Corrientes 1234", bbox=(-31.0, -32.0, -63.0, -65.0))
    assert r.has_coords
    assert r.lat == pytest.approx(-31.4201, abs=1e-2)


def test_el_pincode_pesa_en_la_busqueda(index):
    r = LocalOSMGeocoder(index).geocode("Shanti Nagar, Mumbai 400069")
    assert r.status in (STATUS_MATCHED, STATUS_LOW)
    assert r.lat == pytest.approx(19.1197, abs=1e-2)


def test_indice_inexistente_falla_claro(tmp_path):
    with pytest.raises(FileNotFoundError, match="build-geocoder-index"):
        LocalOSMGeocoder(tmp_path / "no_existe.sqlite")


# ---------- runner ----------

def _write_normalized(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["delivery_id", "address", "lat", "lng"],
                           lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def test_conserva_las_filas_que_ya_tenian_coordenadas(index, tmp_path):
    from smart_import.geocoding.runner import run

    src = tmp_path / "in.csv"
    _write_normalized(src, [
        {"delivery_id": "A", "address": "otra cosa", "lat": "-34.5", "lng": "-58.4"},
        {"delivery_id": "B", "address": "Av. Corrientes 1234, Buenos Aires", "lat": "", "lng": ""},
    ])
    report = run(src, tmp_path / "out.csv", index, cache_path=tmp_path / "cache.sqlite")

    rows = list(csv.DictReader(open(tmp_path / "out.csv", encoding="utf-8")))
    assert rows[0]["lat"] == "-34.5" and rows[0]["geocode_status"] == STATUS_ALREADY
    assert report.already_geocoded == 1
    assert rows[1]["geocode_status"] == STATUS_MATCHED and rows[1]["lat"]


def test_el_cache_evita_repetir_la_busqueda(index, tmp_path):
    from smart_import.geocoding.runner import run

    src = tmp_path / "in.csv"
    same = "Av. Corrientes 1234, Buenos Aires"
    _write_normalized(src, [{"delivery_id": str(i), "address": same, "lat": "", "lng": ""}
                            for i in range(5)])
    report = run(src, tmp_path / "out.csv", index, cache_path=tmp_path / "cache.sqlite")
    assert report.cache["hits"] == 4 and report.cache["misses"] == 1


def test_columnas_de_diagnostico_siempre_presentes(index, tmp_path):
    from smart_import.geocoding.runner import run

    src = tmp_path / "in.csv"
    _write_normalized(src, [{"delivery_id": "A", "address": "Calle Falsa 123", "lat": "", "lng": ""}])
    run(src, tmp_path / "out.csv", index, cache_path=tmp_path / "cache.sqlite")
    row = next(iter(csv.DictReader(open(tmp_path / "out.csv", encoding="utf-8"))))
    for col in ("geocode_status", "geocode_confidence", "geocode_precision", "geocode_source"):
        assert col in row
    assert row["geocode_status"] == STATUS_NOT_FOUND
    assert row["lat"] == ""          # sin coordenadas inventadas


@pytest.mark.geocoding
def test_low_confidence_lejos_del_depot_no_escribe_coords(index, tmp_path):
    """Match a nivel calle lejos del depot = falso positivo; no inventar coords."""
    from smart_import.config import Config
    from smart_import.geocoding.runner import run

    src = tmp_path / "in.csv"
    _write_normalized(src, [{
        "delivery_id": "A",
        "address": "Av. Corrientes 9999, Buenos Aires",
        "lat": "", "lng": "",
    }])
    cfg = Config.from_env().replace(max_low_confidence_km=15.0)
    # Depot en Cordoba (~700 km de CABA)
    report = run(
        src, tmp_path / "out.csv", index,
        origin=(-31.42, -64.19),
        config=cfg,
        cache_path=tmp_path / "cache.sqlite",
    )
    row = next(iter(csv.DictReader(open(tmp_path / "out.csv", encoding="utf-8"))))
    assert report.not_found == 1
    assert report.low_confidence == 0
    assert row["lat"] == "" and row["lng"] == ""
    assert row["geocode_status"] == STATUS_NOT_FOUND


@pytest.mark.geocoding
def test_low_confidence_cerca_del_depot_se_conserva(index, tmp_path):
    from smart_import.config import Config
    from smart_import.geocoding.runner import run

    src = tmp_path / "in.csv"
    _write_normalized(src, [{
        "delivery_id": "A",
        "address": "Av. Corrientes 9999, Buenos Aires",
        "lat": "", "lng": "",
    }])
    cfg = Config.from_env().replace(max_low_confidence_km=15.0)
    report = run(
        src, tmp_path / "out.csv", index,
        origin=(-34.60, -58.38),
        config=cfg,
        cache_path=tmp_path / "cache.sqlite",
    )
    row = next(iter(csv.DictReader(open(tmp_path / "out.csv", encoding="utf-8"))))
    # Contadores siguen la banda (valid/review), no el status crudo low_confidence.
    assert report.matched + report.low_confidence == 1
    assert row["lat"] and row["lng"]
    assert row["geocode_status"] == STATUS_LOW
    assert row["geocode_band"] in ("valid", "review")


@pytest.mark.geocoding
def test_runner_enriquece_query_con_depot(index, tmp_path, monkeypatch):
    """Sin ciudad en la fila, el runner geocodifica con tokens del depot."""
    from smart_import.config import Config
    from smart_import.geocoding import osm_geocoder as og
    from smart_import.geocoding.depot_context import DepotContext
    from smart_import.geocoding.runner import run

    seen: list[str] = []
    real = og.LocalOSMGeocoder.geocode

    def wrap(self, address, origin=None, bbox=None):
        seen.append(address)
        return real(self, address, origin=origin, bbox=bbox)

    monkeypatch.setattr(og.LocalOSMGeocoder, "geocode", wrap)

    src = tmp_path / "in.csv"
    _write_normalized(src, [{
        "delivery_id": "A",
        "address": "Av. Corrientes 1234",
        "lat": "", "lng": "",
    }])
    depot = DepotContext(
        lat=-34.60, lon=-58.38,
        city="CABA", region="Buenos Aires",
        max_distance_km=500.0,
    )
    report = run(
        src, tmp_path / "out.csv", index, depot=depot,
        config=Config.from_env(), cache_path=tmp_path / "cache_enrich.sqlite",
    )
    assert seen and "CABA" in seen[0] and "Buenos Aires" in seen[0]
    assert report.enriched == 1
    # El address VISIBLE no se muta: el enrich solo vive en la query interna
    import csv
    with open(tmp_path / "out.csv", encoding="utf-8") as fh:
        out_row = next(csv.DictReader(fh))
    assert out_row["address"] == "Av. Corrientes 1234"
    assert "CABA" not in out_row["address"]


def test_la_altura_entra_en_la_consulta_fts(index):
    """Sin la altura, `"florida" OR "buenos" OR "aires"` ordenado por bm25 devolvia
    40 POIs llamados "Florida" y NINGUNA fila de la calle Florida: los documentos
    de direccion son mas largos y bm25 los castiga."""
    from smart_import.geocoding.address import parse

    g = LocalOSMGeocoder(index)
    p = parse("Avenida Corrientes 1234, Buenos Aires")

    precisa = g._precise_query(p)
    assert precisa is not None
    assert '"1234"' in precisa and "AND" in precisa
    assert "corrientes" in precisa
    assert "argentina" not in precisa
    assert "avenida" not in precisa

    amplia = g._fts_query(p)
    assert "1234" not in amplia          # la amplia es la red de contencion
    assert "argentina" not in amplia


def test_fts_no_usa_el_pais_anexado_por_el_depot(index):
    from smart_import.geocoding.address import parse

    g = LocalOSMGeocoder(index)
    p = parse("AV CORRIENTES 919, Argentina, CABA")
    q = g._precise_query(p)
    assert q is not None
    assert '"919"' in q and "corrientes" in q
    assert "argentina" not in q and "caba" not in q


def test_fts_busca_el_dia_en_calles_fecha(index):
    from smart_import.geocoding.address import parse

    g = LocalOSMGeocoder(index)
    p = parse("11 de septiembre 1735, CABA, Argentina")
    words = g._search_words(p)
    assert "11" in words and "septiembre" in words
    amplia = g._fts_query(p)
    assert '"11"' in amplia and '"septiembre"' in amplia
    assert " AND " in amplia


def test_fts_grilla_us_no_or_el_cuadrante_con_el_ordinal(index):
    """'350 NE 1st' no puede MATCH-ear '350 Northeast 71st Street'."""
    from smart_import.geocoding.address import parse

    g = LocalOSMGeocoder(index)
    p = parse("350 NE 1st Ave, Miami, United States")
    q = g._precise_query(p)
    assert q is not None
    assert "350" in q and "1st" in q
    assert " AND " in q
    # el ordinal no va en el mismo OR que northeast
    assert 'northeast" OR "1st"' not in q
    assert '"1st" OR "noreste"' not in q
    assert '"1st" OR "ne"' not in q


def test_calle_us_1st_no_es_71st():
    from smart_import.geocoding.address import parse, normalize_text
    from smart_import.geocoding.scoring import _street_score

    p = parse("350 NE 1st Ave, Miami")
    assert _street_score(p, normalize_text("Northeast 71st Street"), p.normalized) == 0.0
    assert _street_score(p, normalize_text("Northwest 1st Avenue"), p.normalized) == 0.0
    assert _street_score(p, normalize_text("Northeast 1st Avenue"), p.normalized) == 1.0


def test_brickell_no_es_brickell_key():
    from smart_import.geocoding.address import parse, normalize_text
    from smart_import.geocoding.scoring import _street_score

    p = parse("801 Brickell Ave, Miami")
    ave = _street_score(p, normalize_text("Brickell Avenue"), p.normalized)
    key = _street_score(p, normalize_text("Brickell Key Boulevard"), p.normalized)
    assert ave == 1.0
    assert key < ave


def test_elige_1st_ave_no_71st_aunque_la_altura_350_este_en_71st(tmp_path):
    """El caso Miami: misma altura en otra calle de la grilla, 7 km al norte."""
    ruta = tmp_path / "grid.sqlite"
    conn = sqlite3.connect(ruta)
    conn.executescript(SCHEMA_SQL)
    filas = [
        ("node", 1, 25.83994, -80.18957, "building", None, "350",
         "Northeast 71st Street",
         "350 northeast 71st street miami fl 33138"),
        ("node", 2, 25.77766, -80.19247, "building", None, "300",
         "Northeast 1st Avenue",
         "300 northeast 1st avenue miami fl 33132"),
    ]
    for osm_type, osm_id, lat, lon, kind, name, num, street, texto in filas:
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, kind, name, house_number,"
            " street, city, district, state, postcode, country, normalized_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?)",
            (osm_type, osm_id, lat, lon, kind, name, num, street,
             "Miami", normalize_text(texto)),
        )
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) "
                 "SELECT id, normalized_text FROM places")
    conn.execute("INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon) "
                 "SELECT id, lat, lat, lon, lon FROM places")
    conn.commit()
    conn.close()

    r = LocalOSMGeocoder(ruta).geocode(
        "350 NE 1st Ave, Miami, United States",
        origin=(25.77427, -80.19366),
    )
    assert r.has_coords
    assert r.lat == pytest.approx(25.77766, abs=1e-4)
    assert "71st" not in (r.matched_text or "")
    assert r.precision == "street"  # OSM no tiene 350 en 1st → Review, no Valid


def test_localidad_en_el_road_no_tumba_la_calle():
    """El parser deja 'Miami Beach' en road; OSM solo tiene 'Collins Avenue'."""
    from smart_import.geocoding.address import parse, normalize_text
    from smart_import.geocoding.scoring import _street_score

    p = parse("1500 Collins Ave Miami Beach, United States")
    assert _street_score(p, normalize_text("Collins Avenue"), p.normalized) == 1.0


def test_fts_altura_sin_ceros_a_la_izquierda(index):
    from smart_import.geocoding.address import parse

    g = LocalOSMGeocoder(index)
    p = parse("AV JUAN DE GARAY 03845, CABA, Argentina")
    q = g._precise_query(p)
    assert q is not None
    assert "3845" in q


def test_caba_cuenta_como_ciudad_autonoma():
    from smart_import.geocoding.address import parse
    from smart_import.geocoding.scoring import Candidate, score

    parsed = parse("AV CORRIENTES 919, CABA, Argentina")
    caba = Candidate(
        1, -34.6034, -58.3796, "building", None, "902", "Avenida Corrientes",
        "Ciudad Autónoma de Buenos Aires", None, None, None, "Argentina",
        "902 avenida corrientes",
    )
    _, parts = score(parsed, caba, origin=(-34.6037, -58.3816))
    assert parts["locality"] == 1.0
    assert parts["house_number"] == 0.55  # 919 vs 902, cercana

    pba = Candidate(
        2, -34.64, -58.56, "building", None, "902", "Corrientes",
        "Ramos Mejía", None, "Buenos Aires", None, "Argentina",
        "902 corrientes ramos mejia",
    )
    _, pba_parts = score(parsed, pba)
    assert pba_parts["locality"] == 0.0


def test_altura_ignora_ceros_a_la_izquierda():
    from smart_import.geocoding.address import parse
    from smart_import.geocoding.scoring import Candidate, score

    parsed = parse("AV JUAN DE GARAY 03845, CABA")
    cand = Candidate(
        1, -34.63, -58.41, "building", None, "3845", "Avenida Juan de Garay",
        "Ciudad Autónoma de Buenos Aires", None, None, None, None,
        "3845 avenida juan de garay",
    )
    _, parts = score(parsed, cand)
    assert parts["house_number"] == 1.0


def test_elige_altura_cercana_no_el_bm25_de_la_avenida(tmp_path):
    """bm25 de 'corrientes' prioriza 1-40; 919 vive cerca de 902."""
    ruta = tmp_path / "avenida.sqlite"
    conn = sqlite3.connect(ruta)
    conn.executescript(SCHEMA_SQL)
    # 40 alturas bajas (ganan bm25) + la cercana a 919
    filas = [
        ("node", i, -34.6037, -58.3700 + i * 0.00001, "building", None,
         str(i), "Avenida Corrientes",
         f"{i} avenida corrientes ciudad autonoma de buenos aires")
        for i in range(1, 41)
    ]
    filas.append((
        "node", 902, -34.60369, -58.37959, "building", None, "902",
        "Avenida Corrientes",
        "902 avenida corrientes ciudad autonoma de buenos aires",
    ))
    for osm_type, osm_id, lat, lon, kind, name, num, street, texto in filas:
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, kind, name, house_number,"
            " street, city, district, state, postcode, country, normalized_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?)",
            (osm_type, osm_id, lat, lon, kind, name, num, street,
             "Ciudad Autónoma de Buenos Aires", normalize_text(texto)),
        )
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) "
                 "SELECT id, normalized_text FROM places")
    conn.execute("INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon) "
                 "SELECT id, lat, lat, lon, lon FROM places")
    conn.commit()
    conn.close()

    r = LocalOSMGeocoder(ruta).geocode(
        "AV CORRIENTES 919, CABA, Argentina",
        origin=(-34.6037, -58.3816),
    )
    assert r.has_coords
    assert r.lat == pytest.approx(-34.60369, abs=1e-4)
    assert r.precision == "street"  # 902 != 919: review, no valid


def test_sin_altura_solo_queda_la_consulta_amplia(index):
    from smart_import.geocoding.address import parse

    g = LocalOSMGeocoder(index)
    assert g._precise_query(parse("Shanti Nagar, Mumbai")) is None
    assert g._fts_query(parse("Shanti Nagar, Mumbai"))


def test_encuentra_la_direccion_aunque_haya_POIs_con_el_mismo_nombre(index, tmp_path):
    """Regresion: el POI homonimo no puede tapar a la calle con altura."""
    import sqlite3

    from smart_import.geocoding.address import normalize_text
    from smart_import.geocoding.osm_index import SCHEMA_SQL

    ruta = tmp_path / "poi.sqlite"
    conn = sqlite3.connect(ruta)
    conn.executescript(SCHEMA_SQL)
    filas = [("node", i, -34.60, -58.37, "bus_stop", "Florida", None, None, "florida")
             for i in range(1, 51)]                      # 50 POIs llamados "Florida"
    filas.append(("node", 99, -34.6024, -58.3753, "building", None, "500", "Florida",
                  "500 florida buenos aires"))           # la direccion real
    for osm_type, osm_id, lat, lon, kind, name, num, street, texto in filas:
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, kind, name, house_number,"
            " street, city, district, state, postcode, country, normalized_text)"
            " VALUES (?,?,?,?,?,?,?,?,'Buenos Aires',NULL,NULL,NULL,NULL,?)",
            (osm_type, osm_id, lat, lon, kind, name, num, street,
             normalize_text(texto + " buenos aires")))
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) "
                 "SELECT id, normalized_text FROM places")
    conn.execute("INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon) "
                 "SELECT id, lat, lat, lon, lon FROM places")
    conn.commit()
    conn.close()

    r = LocalOSMGeocoder(ruta).geocode("Florida 500, Buenos Aires")
    assert r.status == STATUS_MATCHED
    assert r.precision == "housenumber"
    assert r.lat == pytest.approx(-34.6024, abs=1e-3)


# ---------- el cache no puede tapar una mejora del geocoder ----------

def test_una_entrada_de_version_anterior_se_ignora(tmp_path):
    """Sin esto, una mejora del geocoder es invisible hasta borrar el cache."""
    from smart_import.geocoding import cache as cache_mod
    from smart_import.geocoding.base import GeocodeResult

    ruta = tmp_path / "c.sqlite"
    viejo = cache_mod.GeocodeCache(ruta)
    viejo.put("Florida 500", GeocodeResult(status="not_found", confidence=0.12), "ar")
    viejo.commit()
    viejo.close()

    # el geocoder mejora y sube su version
    original = cache_mod.GEOCODER_VERSION
    cache_mod.GEOCODER_VERSION = original + 1
    try:
        nuevo = cache_mod.GeocodeCache(ruta)
        assert nuevo.get("Florida 500", "ar") is None      # se ignora, se reconsulta
        assert nuevo.stats()["misses"] == 1  # obsolete versions are purged at open
        nuevo.close()
    finally:
        cache_mod.GEOCODER_VERSION = original


def test_un_cache_viejo_sin_la_columna_version_se_migra(tmp_path):
    """Un cache ya existente en produccion no puede romper el arranque."""
    import sqlite3

    from smart_import.geocoding.cache import GeocodeCache

    ruta = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(ruta)
    conn.executescript(
        "CREATE TABLE geocode_cache (key TEXT PRIMARY KEY, normalized_address TEXT NOT NULL,"
        " lat REAL, lon REAL, status TEXT NOT NULL, confidence REAL, precision TEXT,"
        " source TEXT, created_at REAL NOT NULL);")
    conn.execute("INSERT INTO geocode_cache VALUES ('k','x',1.0,2.0,'matched',1.0,'s','osm',0)")
    conn.commit()
    conn.close()

    c = GeocodeCache(ruta)                                  # no debe explotar
    columnas = {r[1] for r in c._conn.execute("PRAGMA table_info(geocode_cache)")}
    assert "version" in columnas
    c.close()


def test_el_cache_sirve_una_entrada_de_la_version_actual(tmp_path):
    from smart_import.geocoding.base import STATUS_MATCHED, GeocodeResult
    from smart_import.geocoding.cache import GeocodeCache

    ruta = tmp_path / "c.sqlite"
    c = GeocodeCache(ruta)
    c.put("Av. Corrientes 1234", GeocodeResult(status=STATUS_MATCHED, lat=-34.6, lon=-58.4,
                                               confidence=1.0), "ar")
    c.commit()
    hit = c.get("Av. Corrientes 1234", "ar")
    assert hit is not None and hit.lat == -34.6
    assert c.stats()["stale"] == 0
    c.close()
