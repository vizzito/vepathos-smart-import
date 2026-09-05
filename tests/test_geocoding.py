"""Geocoding: registry de PBF, normalizacion de direcciones, scoring y runner.

El indice se arma sinteticamente para que la suite no dependa de un .osm.pbf de
25 MB. Los tests contra un PBF real viven en test_geocoding_real.py y se saltan
si no hay extracts disponibles.
"""
import csv
import sqlite3

import pytest

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

    amplia = g._fts_query(p)
    assert "1234" not in amplia          # la amplia es la red de contencion


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
