"""JSON reader resiliente: repara / salva / no tumba el import entero."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from smart_import.config import Config
from smart_import.pipeline import run_normalize
from smart_import.readers.json_reader import (
    loads_resilient,
    read,
    repair_json_text,
)

SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "vepathos_flat_v1.json"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_repair_trailing_dot_number():
    text = '{"lng": -71., "lat": 42.44}'
    fixed, fixes = repair_json_text(text)
    assert "decimal truncado" in fixes[0]
    assert json.loads(fixed) == {"lng": -71.0, "lat": 42.44}


def test_loads_resilient_trailing_dot_en_address(tmp_path):
    """Caso dbo1: un lng truncado no tumba las demas addresses."""
    path = _write(tmp_path, "broken_lng.json", """{
  "addresses": [
    {
      "lat": 42.44, "lng": -71.15,
      "address": "Ok St", "packages": [{"package_id": "A", "weight_kg": 1}]
    },
    {
      "lat": 42.440656, "lng": -71.,
      "address": "7212 Warren St", "packages": [{"package_id": "B", "weight_kg": 1.1}]
    },
    {
      "lat": 42.45, "lng": -71.16,
      "address": "Other St", "packages": [{"package_id": "C", "weight_kg": 2}]
    }
  ]
}
""")
    table = read(path)
    assert len(table.rows) == 3
    assert any("JSON reparado" in n for n in table.meta.notes)
    # round-trip normalize
    result = run_normalize(
        path, SCHEMA, tmp_path / "out.csv",
        emit=("flat", "nested"), config=Config.from_env(), phone_region="US",
    )
    assert result.report["deliveries"] == 3
    assert result.report["packages"] == 3
    assert result.report["valid_rows"] == 3


def test_loads_resilient_trailing_comma(tmp_path):
    path = _write(tmp_path, "comma.json", """{
  "addresses": [
    {"lat": 1.0, "lng": 2.0, "address": "A", "packages": [{"package_id": "1"}],},
  ],
}
""")
    table = read(path)
    assert len(table.rows) == 1
    assert any("reparado" in n or "trailing comma" in n for n in table.meta.notes)


def test_salvage_descarta_solo_address_irrecuperable(tmp_path):
    """Un objeto con basura irrecuperable se saltea; el resto sigue (incluso despues)."""
    path = _write(tmp_path, "salvage.json", """{
  "addresses": [
    {"lat": 40.7, "lng": -74.0, "address": "Good 1", "packages": [{"package_id": "1"}]},
    {"lat": 40.8, "lng": -74.1, "address": "BROKEN "unterminated, "packages": []},
    {"lat": 40.9, "lng": -74.2, "address": "Good 2", "packages": [{"package_id": "2"}]}
  ]
}
""")
    table = read(path)
    assert len(table.rows) == 2
    notes = " ".join(table.meta.notes)
    assert "salvamento" in notes or "reparado" in notes or "basura" in notes or "omitido" in notes
    result = run_normalize(
        path, SCHEMA, tmp_path / "out.csv",
        emit=("nested",), config=Config.from_env(), phone_region="US",
    )
    assert result.report["deliveries"] == 2
    addrs = {a["address"] for a in json.loads(Path(result.outputs["nested"]).read_text())["addresses"]}
    assert addrs == {"Good 1", "Good 2"}


def test_boston_corrupt_sample_recupera_las_sanas():
    """Fixture deliberadamente destrozado: smart no 422; salva addresses parseables."""
    path = Path(__file__).resolve().parents[1] / "fixtures" / "boston_corrupt_sample.json"
    assert path.exists()
    table = read(path)
    # 12 addresses sanas; fragmentos Cambridge/Worcester + Boylston truncado descartados
    assert len({row[table.columns.index("delivery_id")]
                for row in table.rows
                if "delivery_id" in table.columns}) >= 11
    notes = " ".join(table.meta.notes)
    assert "reparado" in notes or "salvamento" in notes

    from smart_import.pipeline import run_normalize as rn
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        result = rn(
            path, SCHEMA, Path(td) / "out.csv",
            emit=("flat", "nested"), config=Config.from_env(), phone_region="US",
        )
        assert result.report["deliveries"] == 12
        assert result.report["packages"] >= 20
        nested = json.loads(Path(result.outputs["nested"]).read_text())["addresses"]
        texts = {(a.get("address") or "") for a in nested}
        assert any("Warren" in t for t in texts)
        assert any("Washington St, Boston, MA 02114" in t for t in texts)
        assert any("Centre St" in t for t in texts)
        assert not any("Boylston" in t for t in texts)
        assert not any("ambridge" in t.lower() for t in texts)


def test_json_sin_arreglo_posible_sigue_fallando():
    with pytest.raises(json.JSONDecodeError):
        loads_resilient("{not json at all")


def test_coords_fuera_de_rango_en_json_se_rechazan_no_tumba(tmp_path):
    path = _write(tmp_path, "bad_coords.json", """{
  "addresses": [
    {"delivery_id": "OK", "lat": 40.7, "lng": -74.0, "address": "A",
     "packages": [{"package_id": "1"}]},
    {"delivery_id": "BAD", "lat": 95.0, "lng": 200.0, "address": "B",
     "packages": [{"package_id": "2"}]}
  ]
}
""")
    result = run_normalize(
        path, SCHEMA, tmp_path / "out.csv",
        emit=("flat",), config=Config.from_env(), phone_region="US", diagnostics=True,
    )
    assert result.report["deliveries"] == 2
    assert result.report["rejected_coordinates"] == 1
    assert result.report["needs_geocode"] >= 1  # BAD pierde coords → geocode por address


def test_coords_invertidas_en_json_avisan(tmp_path):
    """lat=-122 / lng=47 (Seattle cruzado): warning, no correccion silenciosa."""
    path = _write(tmp_path, "swapped.json", """{
  "addresses": [
    {"delivery_id": "SW", "lat": -122.33, "lng": 47.60,
     "address": "350 5th Ave, New York, NY",
     "packages": [{"package_id": "1"}]}
  ]
}
""")
    result = run_normalize(
        path, SCHEMA, tmp_path / "out.csv",
        emit=("flat",), config=Config.from_env(), phone_region="US",
    )
    assert any("invertid" in w for w in result.report["warnings"])
    assert result.report["rejected_coordinates"] >= 1
    assert result.report["needs_geocode"] == 1


def test_address_sin_lat_lng_queda_para_geocode(tmp_path):
    path = _write(tmp_path, "no_coords.json", """{
  "addresses": [
    {"delivery_id": "G", "address": "350 5th Ave, New York, NY",
     "packages": [{"package_id": "1", "weight_kg": 1}]}
  ]
}
""")
    result = run_normalize(
        path, SCHEMA, tmp_path / "out.csv",
        emit=("flat",), config=Config.from_env(), phone_region="US",
    )
    assert result.report["deliveries"] == 1
    assert result.report["valid_rows"] == 0
    assert result.report["needs_geocode"] == 1
