"""Clave de la cache de geocode (2026-09-15).

Dos fallas, las dos silenciosas:

* el contexto llevaba el mtime del indice y el geocoder lo toca cada vez que lo
  abre (TTL de indices): dos imports seguidos del mismo archivo daban 0 hits;
* la direccion entraba por `normalize_text`, que borra las comas con las que el
  parser corta segmentos: '4 Стрелча Sofia' heredaba el not_found de la fila
  '4 Стрелча' (consulta '4 Стрелча, Sofia, Bulgaria'), o sea el resultado de una
  fila dependia de las filas anteriores del archivo.

Indice sintetico: la grilla de Tandil de test_geocode_rescue.
"""
from __future__ import annotations

import os
import shutil
import sqlite3

import pytest

from smart_import.geocoding.cache import make_key
from smart_import.geocoding.osm_index import index_identity, touch_index
from tests.test_geocode_rescue import H_STREETS, _run, index_path  # noqa: F401

pytestmark = pytest.mark.geocoding

COLUMNAS = ("geocode_band", "geocode_status", "geocode_confidence", "geocode_precision",
            "geocode_reason", "lat", "lng")


def _filas(rows):
    return [{c: r[c] for c in ("address", *COLUMNAS)} for r in rows]


# ---------- entre imports ----------

def test_abrir_el_indice_no_cambia_su_identidad(tmp_path, index_path):
    indice = tmp_path / "indice.sqlite"
    shutil.copyfile(index_path, indice)
    antes = index_identity(indice)
    os.utime(indice, ns=(0, 0))
    touch_index(indice)
    assert index_identity(indice) == antes


def test_reconstruir_el_indice_cambia_su_identidad(tmp_path, index_path):
    """`build` escribe un temporal y hace os.replace: mismo contenido, otro archivo."""
    indice = tmp_path / "indice.sqlite"
    shutil.copyfile(index_path, indice)
    antes = index_identity(indice)
    nuevo = tmp_path / "indice.sqlite.tmp.1"
    shutil.copyfile(index_path, nuevo)
    os.replace(nuevo, indice)
    assert index_identity(indice) != antes


def test_dos_imports_seguidos_reusan_la_cache(tmp_path, index_path):
    direcciones = ["Sarmiento 500", "alvarado 500", "Pellegrini y Sarmiento",
                   "Lisandro de la Torre 400"]
    primero, filas_1 = _run(tmp_path, index_path, direcciones)
    segundo, filas_2 = _run(tmp_path, index_path, direcciones)
    consultas = primero.cache["hits"] + primero.cache["misses"]
    assert primero.cache["misses"] > 0
    assert (segundo.cache["hits"], segundo.cache["misses"]) == (consultas, 0)
    assert _filas(filas_2) == _filas(filas_1)


def test_reemplazar_el_indice_invalida_la_cache(tmp_path, index_path):
    indice = tmp_path / "indice.sqlite"
    shutil.copyfile(index_path, indice)
    _, antes = _run(tmp_path, indice, ["Sarmiento 500"])
    assert float(antes[0]["lng"]) == pytest.approx(-59.1384, abs=1e-6)

    # rebuild atomico como `build`, con la altura 500 de Sarmiento corrida ~35 m
    nuevo = tmp_path / "indice.sqlite.tmp.1"
    shutil.copyfile(indice, nuevo)
    conn = sqlite3.connect(nuevo)
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM places WHERE street = 'Sarmiento' AND house_number = '500'")]
    assert len(ids) == 1
    conn.execute("UPDATE places SET lon = -59.1380 WHERE id = ?", ids)
    conn.execute("UPDATE places_rtree SET min_lon = -59.1380, max_lon = -59.1380 WHERE id = ?", ids)
    conn.commit()
    conn.close()
    os.replace(nuevo, indice)

    reporte, despues = _run(tmp_path, indice, ["Sarmiento 500"])
    assert reporte.cache["hits"] == 0
    assert float(despues[0]["lat"]) == pytest.approx(H_STREETS["Sarmiento"], abs=1e-6)
    assert float(despues[0]["lng"]) == pytest.approx(-59.1380, abs=1e-6)


# ---------- dentro de un archivo ----------

@pytest.mark.parametrize("a,b", [
    ("4 Стрелча, Sofia, Bulgaria", "4 Стрелча Sofia, Bulgaria"),          # osm_bg_sofia
    ("Lisandro, de la Torre 400", "Lisandro de la Torre 400"),
    ("Corrientes 1234; CABA", "Corrientes 1234, CABA"),
    ("Ossington Ave - 38, Toronto, Canada", "Ossington Ave 38, Toronto, Canada"),
    ("SAN JUAN AV.1733, CABA, Argentina", "SAN JUAN AV. 1733, CABA, Argentina"),
    ("Corrientes Nº1234", "Corrientes No1234"),
    ("ถนนสวนพลู 616, Bangkok", "ถนนสวนพล 616, Bangkok"),                # la marca es la vocal
])
def test_la_clave_separa_lo_que_el_parser_lee_distinto(a, b):
    assert make_key(a, "ctx") != make_key(b, "ctx")


@pytest.mark.parametrize("a,b", [
    ("Av. Corrientes 1234", "AV CORRIENTES 1234"),
    ("Entre Ríos 500, Tandil", "ENTRE RIOS 500,  Tandil"),
])
def test_la_clave_junta_mayusculas_tildes_y_abreviaturas(a, b):
    assert make_key(a, "ctx") == make_key(b, "ctx")


def test_una_coma_no_hereda_el_resultado_de_otra_fila(tmp_path, index_path):
    """'Lisandro, de la Torre 400' sola sale ambar (la calle no coincide); despues de
    'Lisandro de la Torre 400' salia verde: heredaba el resultado de la otra fila."""
    (tmp_path / "sola").mkdir()
    (tmp_path / "juntas").mkdir()
    _, sola = _run(tmp_path / "sola", index_path, ["Lisandro, de la Torre 400"])
    reporte, juntas = _run(tmp_path / "juntas", index_path,
                           ["Lisandro de la Torre 400", "Lisandro, de la Torre 400"])
    assert reporte.cache["hits"] == 0
    assert _filas(juntas)[1] == _filas(sola)[0]
