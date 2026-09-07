"""Compatibilidad nested Vepathos (Boston-style) ↔ schema vepathos_flat_v1."""
import json
from pathlib import Path

import pytest

from smart_import.config import Config
from smart_import.pipeline import run_normalize
from smart_import.readers.json_reader import read

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "boston_nested_compat.json"
FIXTURE_FULL = ROOT / "fixtures" / "boston_nested_full.json"
SCHEMA = ROOT / "schemas" / "vepathos_flat_v1.json"


@pytest.fixture
def boston_result(tmp_path):
    assert FIXTURE.exists(), FIXTURE
    return run_normalize(
        FIXTURE, SCHEMA, tmp_path / "out.csv",
        emit=("flat", "nested"), config=Config.from_env(), phone_region="US",
    )


def test_boston_sin_delivery_id_agrupa_packages_del_padre(boston_result):
    """El JSON de producto no trae delivery_id: 3 addresses → 3 stops, no 5."""
    assert boston_result.report["deliveries"] == 3
    assert boston_result.report["packages"] == 5
    notes = " ".join(boston_result.report["input"].get("notes") or [])
    assert "delivery_id sintetico" in notes


def test_boston_preserva_peso_dims_packaging_currency(boston_result):
    nested = json.loads(Path(boston_result.outputs["nested"]).read_text())["addresses"]
    first = next(a for a in nested if "Harrison" in (a.get("address") or ""))
    assert len(first["packages"]) == 2
    p0 = first["packages"][0]
    assert p0["weight_kg"] == 3.2
    assert p0["dimensions"] == {"length": 49.5, "width": 35.6, "height": 7.6}
    assert p0["packaging"] == "BOX"
    assert p0["value_cents"] == 0
    assert p0.get("value_currency") == "USD"
    assert p0["status"] == "PENDING"


def test_boston_preserva_time_window(boston_result):
    nested = json.loads(Path(boston_result.outputs["nested"]).read_text())["addresses"]
    albany = next(a for a in nested if "Albany" in (a.get("address") or ""))
    assert len(albany["packages"]) == 2
    tw = albany["packages"][0]["time_window"]
    assert tw["start"] == "2018-07-24 13:00"
    assert tw["end"] == "2018-07-24 21:00"
    assert tw["time_zone"] == "UTC"


def test_boston_value_currency_mapea(boston_result):
    mapped = {m.target for m in boston_result.mapping.mapping.values()}
    assert "value_currency" in mapped
    assert "value_currency" not in (boston_result.report.get("unmapped") or [])


def test_json_reader_sintetiza_id_por_address():
    table = read(FIXTURE)
    assert "delivery_id" in table.columns
    ids = {row[table.columns.index("delivery_id")] for row in table.rows}
    assert ids == {"DLV-00001", "DLV-00002", "DLV-00003"}
    assert len(table.rows) == 5  # flat: 1 fila por package


def test_boston_full_normal_y_smart_mismo_camino(tmp_path):
    """JSON nested producto (sin delivery_id): 52 stops / 77 pkgs, 0 unmapped.

    Tanto el import 'normal' (JSON ya shaped) como el smart-import pasan por
    `run_normalize` + json_reader; no hay camino free-text/IA para .json.
    """
    assert FIXTURE_FULL.exists(), FIXTURE_FULL
    result = run_normalize(
        FIXTURE_FULL, SCHEMA, tmp_path / "full.csv",
        emit=("flat", "nested"), config=Config.from_env(), phone_region="US",
    )
    assert result.report["deliveries"] == 52
    assert result.report["packages"] == 77
    assert result.report["valid_rows"] == 77
    assert result.report["needs_geocode"] == 0
    assert result.report["invalid_rows"] == 0
    assert not result.report.get("unmapped")
    assert not result.report.get("needs_review")
    nested = json.loads(Path(result.outputs["nested"]).read_text())["addresses"]
    assert len(nested) == 52
    with_tw = [
        a for a in nested
        if any(isinstance(p.get("time_window"), dict) for p in a.get("packages") or [])
    ]
    assert len(with_tw) >= 1
    currencies = {
        p.get("value_currency") for a in nested for p in a.get("packages") or []
    }
    assert currencies == {"USD"}
