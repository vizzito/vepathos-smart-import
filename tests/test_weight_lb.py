"""Aliases lb/pounds → weight_kg con conversion a kilogramos."""
from __future__ import annotations

import json
from pathlib import Path

from smart_import.normalization.units import column_declares_pounds, pounds_to_kg
from smart_import.pipeline import run_normalize
from tests.conftest import SCHEMA


def test_column_declares_pounds():
    assert column_declares_pounds("weight_lb")
    assert column_declares_pounds("Weight (lbs)")
    assert column_declares_pounds("pounds")
    assert column_declares_pounds("peso libras")
    assert not column_declares_pounds("weight_kg")
    assert not column_declares_pounds("weight")
    assert not column_declares_pounds("peso")
    assert not column_declares_pounds("Kg")


def test_pounds_to_kg():
    assert pounds_to_kg(10) == round(10 * 0.45359237, 6)


def test_csv_weight_lb_convierte_a_kg(tmp_path):
    path = tmp_path / "lb.csv"
    path.write_text(
        "delivery_id,lat,lng,address,package_id,weight_lb\n"
        "D1,42.44,-71.15,Swan Rd,P1,10\n"
        "D2,42.45,-71.16,Other St,P2,2.2\n",
        encoding="utf-8",
    )
    result = run_normalize(path, SCHEMA, tmp_path / "out.csv", emit=("flat", "nested"))
    assert "weight_kg" in {m.target for m in result.mapping.mapping.values()}
    nested = json.loads(Path(result.outputs["nested"]).read_text())["addresses"]
    w0 = nested[0]["packages"][0]["weight_kg"]
    w1 = nested[1]["packages"][0]["weight_kg"]
    assert abs(w0 - pounds_to_kg(10)) < 1e-6
    assert abs(w1 - pounds_to_kg(2.2)) < 1e-6
    assert any("libras" in w.lower() for w in (result.report.get("warnings") or []))


def test_csv_weight_kg_no_convierte(tmp_path):
    path = tmp_path / "kg.csv"
    path.write_text(
        "delivery_id,lat,lng,address,package_id,weight_kg\n"
        "D1,42.44,-71.15,Swan Rd,P1,10\n",
        encoding="utf-8",
    )
    result = run_normalize(path, SCHEMA, tmp_path / "out.csv", emit=("nested",))
    nested = json.loads(Path(result.outputs["nested"]).read_text())["addresses"]
    assert nested[0]["packages"][0]["weight_kg"] == 10.0


def test_json_weight_lb_alias(tmp_path):
    path = tmp_path / "lb.json"
    path.write_text(json.dumps({
        "addresses": [{
            "lat": 42.44, "lng": -71.15, "zone": "G-1",
            "packages": [{"package_id": "A", "weight_lb": 22}],
        }],
    }), encoding="utf-8")
    result = run_normalize(path, SCHEMA, tmp_path / "out.csv", emit=("nested",))
    pkg = json.loads(Path(result.outputs["nested"]).read_text())["addresses"][0]["packages"][0]
    assert abs(pkg["weight_kg"] - pounds_to_kg(22)) < 1e-6
