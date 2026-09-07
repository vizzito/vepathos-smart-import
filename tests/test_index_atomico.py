"""El indice OSM se construye entero o no se construye.

Se escribia directo sobre la ruta final tras un unlink(): dos jobs de la misma
ciudad se pisaban, y un OOM-kill o un `docker stop` a mitad dejaba un sqlite
truncado ahi mismo. Como el unico chequeo era exists(), todos los geocodes
siguientes lo usaban en silencio y devolvian menos resultados.
"""
import sqlite3

import pytest

from smart_import.geocoding import osm_index
from smart_import.geocoding.osm_index import SCHEMA_SQL, build, index_is_complete


def _sqlite(path, *, places=0, fts=True, marca=False):
    """Arma un indice de prueba en el estado que se quiera."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    for i in range(places):
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, street, house_number,"
            " normalized_text) VALUES ('node', ?, -34.6, -58.4, 'corrientes', ?, ?)",
            (i, str(100 + i), f"corrientes {100 + i}"))
    if fts and places:
        conn.execute("INSERT INTO places_fts(rowid, normalized_text) "
                     "SELECT id, normalized_text FROM places")
    if marca:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('build_completed','1')")
    conn.commit()
    conn.close()


def test_un_indice_vacio_no_se_da_por_bueno(tmp_path):
    """Build cortado antes de poblar places: el archivo existe y no sirve."""
    idx = tmp_path / "ciudad.sqlite"
    _sqlite(idx, places=0)
    assert not index_is_complete(idx)


def test_un_indice_sin_fts_no_se_da_por_bueno(tmp_path):
    """Build cortado entre places y FTS: el geocoder no encontraria nada."""
    idx = tmp_path / "ciudad.sqlite"
    _sqlite(idx, places=5, fts=False)
    assert not index_is_complete(idx)


def test_un_indice_corrupto_no_se_da_por_bueno(tmp_path):
    idx = tmp_path / "ciudad.sqlite"
    idx.write_bytes(b"no soy sqlite" + b"\0" * osm_index.MIN_INDEX_BYTES)  # basta con superar el filtro de tamano
    assert not index_is_complete(idx)


def test_un_indice_viejo_sin_marca_sigue_sirviendo(tmp_path):
    """Los indices ya construidos en prod no tienen la marca: no se reconstruyen."""
    idx = tmp_path / "ciudad.sqlite"
    _sqlite(idx, places=5, marca=False)
    assert index_is_complete(idx)


def test_un_indice_completo_es_valido(tmp_path):
    idx = tmp_path / "ciudad.sqlite"
    _sqlite(idx, places=5, marca=True)
    assert index_is_complete(idx)


def test_un_build_que_falla_no_deja_indice_ni_temporales(tmp_path, monkeypatch):
    """Lo que rompia: el archivo a medias quedaba en la ruta definitiva."""
    pbf = tmp_path / "ciudad.osm.pbf"
    pbf.write_bytes(b"x" * 2048)
    out = tmp_path / "idx" / "ciudad.sqlite"

    def explota(conn, src, location_index, progress):
        conn.execute("CREATE TABLE a (b TEXT)")     # deja la base a medio armar
        raise MemoryError("OOM a mitad del build")

    monkeypatch.setattr(osm_index, "_fill", explota)
    with pytest.raises(MemoryError):
        build(pbf, out)

    assert not out.exists(), "quedo un indice truncado en la ruta final"
    sobrantes = [p.name for p in out.parent.iterdir() if ".tmp." in p.name]
    assert not sobrantes, f"quedaron temporales huerfanos: {sobrantes}"


def test_el_indice_aparece_recien_cuando_esta_entero(tmp_path, monkeypatch):
    pbf = tmp_path / "ciudad.osm.pbf"
    pbf.write_bytes(b"x" * 2048)
    out = tmp_path / "idx" / "ciudad.sqlite"
    visto = {}

    def llenar(conn, src, location_index, progress):
        visto["habia_indice_durante_el_build"] = out.exists()
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, street, normalized_text)"
            " VALUES ('node', 1, -34.6, -58.4, 'corrientes', 'corrientes 100')")
        conn.execute("INSERT INTO places_fts(rowid, normalized_text) "
                     "SELECT id, normalized_text FROM places")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('build_completed','1')")
        conn.commit()
        return osm_index.BuildStats(nodes=1)

    monkeypatch.setattr(osm_index, "_fill", llenar)
    build(pbf, out)

    assert visto["habia_indice_durante_el_build"] is False
    assert out.exists()
