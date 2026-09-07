"""Extract automatico: si no hay recorte de ciudad, se corta del PBF de pais."""
from pathlib import Path

import pytest

from smart_import.geocoding.extract import (
    ExtractError,
    bbox_filename,
    ensure_geocode_index,
    extract_zone,
    prepare_extract_bbox,
    target_bbox_wsen,
)
from smart_import.geocoding.pbf_registry import _parse


CABA = (-34.6037, -58.3816)
CABA_BBOX = (-34.537, -34.697, -58.356, -58.530)


def _fake_index(path) -> None:
    """Indice sqlite minimo pero VALIDO.

    Un archivo de ceros ya no sirve como doble: `index_is_complete` consulta el
    indice como lo consulta el geocoder, justamente para no volver a usar un
    sqlite truncado por un build interrumpido.
    """
    import sqlite3

    from smart_import.geocoding.osm_index import SCHEMA_SQL

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO places (osm_type, osm_id, lat, lon, street, house_number,"
        " normalized_text) VALUES ('node', 1, -34.60, -58.38, 'corrientes',"
        " '100', 'corrientes 100')")
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) "
                 "SELECT id, normalized_text FROM places")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('build_completed', '1')")
    conn.commit()
    conn.close()



def test_bbox_filename_estable_y_parseable():
    west, south, east, north = -58.6, -34.8, -58.2, -34.4
    name = bbox_filename(west, south, east, north, 0.1)
    assert name == "n-34.40_s-34.80_e-58.20_w-58.60-pyrosm.osm.pbf"
    entry = _parse(Path("/data/extracts/argentina") / name)
    assert entry.has_bbox
    assert entry.key == "n-34.40_s-34.80_e-58.20_w-58.60"


def test_prepare_bbox_desde_punto_caba():
    w, s, e, n = target_bbox_wsen(*CABA, None, margin_km=15.0, max_km=80.0, round_deg=0.1)
    assert w < CABA[1] < e
    assert s < CABA[0] < n
    assert n - s <= 0.9
    assert e - w <= 0.9


def test_prepare_bbox_redondea_hacia_afuera():
    w, s, e, n = prepare_extract_bbox(-58.40, -34.65, -58.35, -34.55, margin_km=0, max_km=80, round_deg=0.1)
    assert w <= -58.40 <= e
    assert s <= -34.65 <= n
    assert w == -58.4
    assert e == -58.3


def test_zona_usa_slug_de_pais():
    assert extract_zone("/data/south-america_tile_x/argentina-pyrosm.osm.pbf", "argentina") == "argentina"
    assert extract_zone("/data/north-america/florida-pyrosm.osm.pbf", "florida") == "florida"


def _country_pbf(root: Path, name: str = "argentina-pyrosm.osm.pbf") -> Path:
    pbf = root / "south-america" / name
    pbf.parent.mkdir(parents=True)
    pbf.write_bytes(b"\0" * 100)
    return pbf


def test_sin_extract_corta_y_no_indexa_el_pais(tmp_path, monkeypatch):
    pbf_dir = tmp_path / "pbf"
    index_dir = tmp_path / "indexes"
    extract_dir = tmp_path / "extracts"
    index_dir.mkdir()
    _country_pbf(pbf_dir)

    cuts: list[Path] = []
    builds: list[tuple[str, str]] = []

    def fake_extract(source, dest, west, south, east, north, osmium_bin=None):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"FAKE-PBF" * 5000)
        cuts.append(dest)

    def fake_build(pbf, output, location_index="flex_mem", progress=None):
        output = Path(output)
        _fake_index(output)
        builds.append((str(Path(pbf).name), str(output.name)))

    monkeypatch.setattr("smart_import.geocoding.extract.run_osmium_extract", fake_extract)
    monkeypatch.setattr("smart_import.geocoding.extract.build", fake_build)

    ready = ensure_geocode_index(
        pbf_dir, index_dir, lat=CABA[0], lon=CABA[1], bbox=CABA_BBOX,
        extract_dir=extract_dir, autobuild=True, autoextract=True,
    )
    assert cuts, "tenia que cortar un extract"
    assert ready.cut_extract
    assert ready.path.name.startswith("n-34.")
    assert "argentina" not in ready.path.name
    assert all("argentina-pyrosm" not in src for src, _ in builds)
    assert all(not name.startswith("argentina") for _, name in builds)
    assert ready.entry.has_bbox


def test_si_ya_hay_indice_de_extract_no_corta(tmp_path, monkeypatch):
    pbf_dir = tmp_path / "pbf"
    index_dir = tmp_path / "indexes"
    extract_dir = tmp_path / "extracts"
    index_dir.mkdir()
    _country_pbf(pbf_dir)
    leftover = index_dir / "n-34.48_s-34.73_e-58.30_w-58.58.sqlite"
    _fake_index(leftover)

    def boom(*_a, **_k):
        raise AssertionError("no debe cortar ni indexar")

    monkeypatch.setattr("smart_import.geocoding.extract.run_osmium_extract", boom)
    monkeypatch.setattr("smart_import.geocoding.extract.build", boom)

    ready = ensure_geocode_index(
        pbf_dir, index_dir, lat=CABA[0], lon=CABA[1], bbox=CABA_BBOX,
        extract_dir=extract_dir,
    )
    assert ready.path == leftover
    assert not ready.cut_extract


def test_si_ya_hay_extract_pbf_indexa_ese(tmp_path, monkeypatch):
    pbf_dir = tmp_path / "pbf"
    index_dir = tmp_path / "indexes"
    extract_dir = tmp_path / "extracts"
    index_dir.mkdir()
    _country_pbf(pbf_dir)
    dest = extract_dir / "argentina" / "n-34.50_s-34.80_e-58.20_w-58.60-pyrosm.osm.pbf"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"\0" * 40_000)

    builds: list[str] = []

    def fake_build(pbf, output, location_index="flex_mem", progress=None):
        _fake_index(output)
        builds.append(Path(pbf).name)

    def boom(*_a, **_k):
        raise AssertionError("no debe volver a cortar")

    monkeypatch.setattr("smart_import.geocoding.extract.run_osmium_extract", boom)
    monkeypatch.setattr("smart_import.geocoding.extract.build", fake_build)

    ready = ensure_geocode_index(
        pbf_dir, index_dir, lat=CABA[0], lon=CABA[1],
        extract_dir=extract_dir,
    )
    assert ready.entry.has_bbox
    assert builds == [dest.name]
    assert ready.path.name == "n-34.50_s-34.80_e-58.20_w-58.60.sqlite"


def test_sin_osmium_no_cae_al_indice_de_pais(tmp_path, monkeypatch):
    pbf_dir = tmp_path / "pbf"
    index_dir = tmp_path / "indexes"
    extract_dir = tmp_path / "extracts"
    index_dir.mkdir()
    _country_pbf(pbf_dir)

    monkeypatch.setattr("smart_import.geocoding.extract.find_osmium", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "smart_import.geocoding.extract.build",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no indexar pais")),
    )

    with pytest.raises(ExtractError, match="osmium"):
        ensure_geocode_index(
            pbf_dir, index_dir, lat=CABA[0], lon=CABA[1],
            extract_dir=extract_dir,
        )
    assert not list(index_dir.glob("argentina.sqlite"))


def test_autoextract_off_no_indexa_el_pais(tmp_path):
    pbf_dir = tmp_path / "pbf"
    index_dir = tmp_path / "indexes"
    index_dir.mkdir()
    _country_pbf(pbf_dir)
    with pytest.raises(FileNotFoundError, match="AUTOEXTRACT"):
        ensure_geocode_index(
            pbf_dir, index_dir, lat=CABA[0], lon=CABA[1],
            extract_dir=tmp_path / "extracts", autoextract=False,
        )
