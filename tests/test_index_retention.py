"""Disco de geocoding: extracts desechables, sqlite caliente vs frío, TTL.

Simula jobs (CABA hoy, NYC hace 20 días, Uruguay en vuelo) sin PBF reales.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from smart_import.geocoding.extract import ensure_geocode_index
from smart_import.geocoding.osm_geocoder import LocalOSMGeocoder
from smart_import.geocoding.osm_index import (
    SCHEMA_SQL,
    drop_indexed_extracts,
    maintain_geocode_disk,
    purge_unused_indexes,
    touch_index,
)

CABA = (-34.6037, -58.3816)
_DAY = 86400


def _index(path: Path, *, with_address: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    if with_address:
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, street, house_number,"
            " normalized_text) VALUES ('node', 1, -34.60, -58.38, 'corrientes',"
            " '100', 'corrientes 100')")
        conn.execute("INSERT INTO places_fts(rowid, normalized_text) "
                     "SELECT id, normalized_text FROM places")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('with_address', '1')")
    else:
        conn.execute(
            "INSERT INTO places (osm_type, osm_id, lat, lon, name, kind,"
            " normalized_text) VALUES ('node', 1, -34.80, -56.20, 'plaza',"
            " 'place', 'plaza')")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('with_address', '0')")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('build_completed', '1')")
    conn.commit()
    conn.close()
    return path


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * _DAY
    os.utime(path, (stamp, stamp))


def _pbf(path: Path, size: int = 40_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


def test_touch_deja_el_indice_caliente(tmp_path):
    idx = _index(tmp_path / "caba.sqlite")
    _age(idx, 20)
    before = idx.stat().st_mtime
    touch_index(idx)
    assert idx.stat().st_mtime > before
    assert time.time() - idx.stat().st_mtime < 5


def test_geocoder_al_abrir_calienta_el_sqlite(tmp_path):
    """El job de geocode (OsmGeocoder) cuenta como uso, no solo ensure."""
    idx = _index(tmp_path / "caba.sqlite")
    _age(idx, 20)
    LocalOSMGeocoder(idx).close()
    assert time.time() - idx.stat().st_mtime < 5


def test_frío_se_borra_caliente_queda(tmp_path):
    root = tmp_path / "indexes"
    caba = _index(root / "n-34.58_s-34.92_e-58.15_w-58.62.sqlite")
    nyc = _index(root / "n41.10_s40.30_e-73.50_w-74.50.sqlite")
    touch_index(caba)
    _age(nyc, 20)
    deleted = purge_unused_indexes(root, ttl_days=14)
    assert nyc in deleted
    assert not nyc.is_file()
    assert caba.is_file()


def test_keep_protege_el_indice_del_job_aunque_este_frio(tmp_path):
    """Uruguay en vuelo: mtime viejo (recién copiado / restore) no se tira."""
    root = tmp_path / "indexes"
    uy = _index(root / "n-34.60_s-35.10_e-55.80_w-56.60-uruguay.sqlite")
    nyc = _index(root / "n41.10_s40.30_e-73.50_w-74.50.sqlite")
    _age(uy, 40)
    _age(nyc, 40)
    deleted = purge_unused_indexes(root, ttl_days=14, keep=uy)
    assert nyc in deleted
    assert uy not in deleted
    assert uy.is_file()
    assert not nyc.is_file()


def test_dos_ciudades_activas_ninguna_caduca(tmp_path):
    root = tmp_path / "indexes"
    caba = _index(root / "n-34.58_s-34.92_e-58.15_w-58.62.sqlite")
    mvd = _index(root / "n-34.60_s-35.10_e-55.80_w-56.60-uruguay.sqlite")
    touch_index(caba)
    touch_index(mvd)
    assert purge_unused_indexes(root, ttl_days=14) == []
    assert caba.is_file() and mvd.is_file()


def test_justo_dentro_del_ttl_sobrevive(tmp_path):
    root = tmp_path / "indexes"
    idx = _index(root / "n-34.58_s-34.92_e-58.15_w-58.62.sqlite")
    _age(idx, 13.5)
    assert purge_unused_indexes(root, ttl_days=14) == []
    assert idx.is_file()


def test_ttl_cero_no_borra_ni_los_de_40_dias(tmp_path):
    root = tmp_path / "indexes"
    idx = _index(root / "n41.10_s40.30_e-73.50_w-74.50.sqlite")
    _age(idx, 40)
    assert purge_unused_indexes(root, ttl_days=0) == []
    assert idx.is_file()


def test_temporal_de_build_no_entra_al_glob_sqlite(tmp_path):
    """osm_index escribe `foo.sqlite.tmp.PID`, que no matchea `*.sqlite`."""
    root = tmp_path / "indexes"
    root.mkdir()
    tmp = root / "n-34.58_s-34.92_e-58.15_w-58.62.sqlite.tmp.99999"
    tmp.write_bytes(b"parcial")
    _age(tmp, 40)
    assert purge_unused_indexes(root, ttl_days=14) == []
    assert tmp.is_file()


def test_borra_extract_propio_si_el_sqlite_sirve(tmp_path):
    extracts = tmp_path / "extracts"
    indexes = tmp_path / "indexes"
    pbf = _pbf(extracts / "uruguay" /
               "n-34.60_s-35.10_e-55.80_w-56.60-uruguay-pyrosm.osm.pbf")
    _index(indexes / "n-34.60_s-35.10_e-55.80_w-56.60-uruguay.sqlite")
    deleted = drop_indexed_extracts(extracts, indexes)
    assert pbf in deleted
    assert not pbf.is_file()


def test_conserva_extract_si_todavia_no_hay_indice(tmp_path):
    extracts = tmp_path / "extracts"
    indexes = tmp_path / "indexes"
    indexes.mkdir()
    pbf = _pbf(extracts / "uruguay" /
               "n-34.60_s-35.10_e-55.80_w-56.60-uruguay-pyrosm.osm.pbf")
    assert drop_indexed_extracts(extracts, indexes) == []
    assert pbf.is_file()


def test_conserva_extract_si_el_indice_no_tiene_calles(tmp_path):
    """El recorte AR sobre Montevideo: sqlite 'completo' de 5 nodos, no usable."""
    extracts = tmp_path / "extracts"
    indexes = tmp_path / "indexes"
    pbf = _pbf(extracts / "argentina" /
               "n-34.60_s-35.10_e-55.90_w-56.40-pyrosm.osm.pbf")
    _index(indexes / "n-34.60_s-35.10_e-55.90_w-56.40.sqlite", with_address=False)
    assert drop_indexed_extracts(extracts, indexes) == []
    assert pbf.is_file()


def test_no_borra_pbf_fuera_de_extract_dir(tmp_path):
    cutter = tmp_path / "pbf" / "_extracts" / "sa"
    ours = tmp_path / "extracts"
    indexes = tmp_path / "indexes"
    name = "n-34.50_s-34.80_e-58.20_w-58.60-pyrosm.osm.pbf"
    alien = _pbf(cutter / name)
    _index(indexes / "n-34.50_s-34.80_e-58.20_w-58.60.sqlite")
    assert drop_indexed_extracts(ours, indexes) == []
    assert alien.is_file()


def test_maintain_tira_pbf_propio_y_sqlite_frio_deja_el_keep(tmp_path):
    extracts = tmp_path / "extracts"
    indexes = tmp_path / "indexes"
    uy = _index(indexes / "n-34.60_s-35.10_e-55.80_w-56.60-uruguay.sqlite")
    nyc = _index(indexes / "n41.10_s40.30_e-73.50_w-74.50.sqlite")
    pbf = _pbf(extracts / "uruguay" /
               "n-34.60_s-35.10_e-55.80_w-56.60-uruguay-pyrosm.osm.pbf")
    _age(nyc, 20)
    touch_index(uy)
    maintain_geocode_disk(indexes, extracts, ttl_days=14, keep=uy)
    assert not pbf.is_file()
    assert uy.is_file()
    assert not nyc.is_file()


def test_segunda_corrida_no_recorta_aunque_ya_no_este_el_pbf(tmp_path, monkeypatch):
    """Sqlite usable → leftover. El extract se puede haber borrado."""
    pbf_dir = tmp_path / "pbf"
    index_dir = tmp_path / "indexes"
    extract_dir = tmp_path / "extracts"
    index_dir.mkdir()
    leftover = index_dir / "n-34.48_s-34.73_e-58.30_w-58.58.sqlite"
    _index(leftover)
    (pbf_dir / "south-america").mkdir(parents=True)
    (pbf_dir / "south-america" / "argentina-pyrosm.osm.pbf").write_bytes(b"\0" * 100)

    def boom(*_a, **_k):
        raise AssertionError("no debe cortar ni indexar: el sqlite alcanza")

    monkeypatch.setattr("smart_import.geocoding.extract.run_osmium_extract", boom)
    monkeypatch.setattr("smart_import.geocoding.extract.build", boom)

    ready = ensure_geocode_index(
        pbf_dir, index_dir, lat=CABA[0], lon=CABA[1],
        extract_dir=extract_dir, index_ttl_days=14,
    )
    assert ready.path == leftover
    assert leftover.is_file()


def test_ensure_borra_el_pbf_propio_y_no_vuelve_a_osmium(tmp_path, monkeypatch):
    pbf_dir = tmp_path / "pbf"
    index_dir = tmp_path / "indexes"
    extract_dir = tmp_path / "extracts"
    index_dir.mkdir()
    (pbf_dir / "south-america").mkdir(parents=True)
    (pbf_dir / "south-america" / "argentina-pyrosm.osm.pbf").write_bytes(b"\0" * 100)

    cuts: list[Path] = []

    def fake_extract(source, dest, west, south, east, north, osmium_bin=None):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"FAKE-PBF" * 5000)
        cuts.append(dest)

    def fake_build(pbf, output, location_index="flex_mem", progress=None):
        _index(Path(output))

    monkeypatch.setattr("smart_import.geocoding.extract.run_osmium_extract", fake_extract)
    monkeypatch.setattr("smart_import.geocoding.extract.build", fake_build)

    first = ensure_geocode_index(
        pbf_dir, index_dir, lat=CABA[0], lon=CABA[1],
        extract_dir=extract_dir, index_ttl_days=14,
    )
    assert cuts and not cuts[0].is_file()
    assert first.path.is_file()

    def no_extract(*_a, **_k):
        raise AssertionError("segunda corrida: osmium no debe correr")

    monkeypatch.setattr("smart_import.geocoding.extract.run_osmium_extract", no_extract)
    monkeypatch.setattr("smart_import.geocoding.extract.build", no_extract)

    second = ensure_geocode_index(
        pbf_dir, index_dir, lat=CABA[0], lon=CABA[1],
        extract_dir=extract_dir, index_ttl_days=14,
    )
    assert second.path == first.path
