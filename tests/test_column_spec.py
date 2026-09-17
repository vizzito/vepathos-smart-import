"""Mapeo manual con unidad y formato: `{campo, unidad, formato}` en español, inglés y portugués."""
from __future__ import annotations

import csv

import pytest

from smart_import.mapping.column_spec import ColumnSpec, ColumnSpecError, parse_column_spec
from smart_import.pipeline import run_normalize
from smart_import.schemas import TargetSchema
from tests.conftest import SCHEMA

TYPES = {f.name: f.type for f in TargetSchema.load(SCHEMA).fields.values()}


@pytest.mark.parametrize(("value", "expected"), [
    ("weight_kg", ColumnSpec("weight_kg")),
    (None, ColumnSpec(None)),
    ({"campo": "weight_kg", "unidad": "Libras"}, ColumnSpec("weight_kg", unit="lb")),
    ({"field": "weight_kg", "unit": "pounds"}, ColumnSpec("weight_kg", unit="lb")),
    ({"campo": "weight_kg", "unidade": "quilogramas"}, ColumnSpec("weight_kg", unit="kg")),
    ({"campo": "weight_kg", "unidad": "gramos"}, ColumnSpec("weight_kg", unit="g")),
    ({"campo": "weight_kg", "unidade": "onças"}, ColumnSpec("weight_kg", unit="oz")),
    ({"field": "length_cm", "unit": "inches"}, ColumnSpec("length_cm", unit="in")),
    ({"campo": "length_cm", "unidad": "pulgadas"}, ColumnSpec("length_cm", unit="in")),
    ({"campo": "height_cm", "unidade": "polegadas"}, ColumnSpec("height_cm", unit="in")),
    ({"campo": "volume_cm3", "unidad": "metros cúbicos"}, ColumnSpec("volume_cm3", unit="m3")),
    ({"campo": "volume_cm3", "unidade": "litros"}, ColumnSpec("volume_cm3", unit="l")),
    ({"field": "volume_cm3", "unit": "cubic feet"}, ColumnSpec("volume_cm3", unit="ft3")),
    ({"campo": "value_cents", "unidad": "pesos"}, ColumnSpec("value_cents", unit="units")),
    ({"campo": "service_time_min", "unidade": "segundos"}, ColumnSpec("service_time_min", unit="s")),
    ({"Campo": "tw_start", "Formato": "dd/mm/aaaa hh:mm"}, ColumnSpec("tw_start", format="%d/%m/%Y %H:%M")),
    ({"field": "tw_start", "format": "MM/DD/YYYY hh:mm AM/PM"}, ColumnSpec("tw_start", format="%m/%d/%Y %I:%M %p")),
    ({"campo": "tw_end", "formato": "mes primero"}, ColumnSpec("tw_end", format="%m/%d/%Y")),
    ({"campo": "tw_end", "formato": "dia primeiro"}, ColumnSpec("tw_end", format="%d/%m/%Y")),
    ({"campo": "weight_kg", "formato": "1.234,56"}, ColumnSpec("weight_kg", format="decimal_comma")),
    ({"campo": "weight_kg", "formato": "vírgula decimal"}, ColumnSpec("weight_kg", format="decimal_comma")),
    ({"field": "weight_kg", "format": "decimal point"}, ColumnSpec("weight_kg", format="decimal_point")),
])
def test_acepta_campo_unidad_y_formato_en_tres_idiomas(value, expected):
    assert parse_column_spec(value, TYPES) == expected


@pytest.mark.parametrize(("value", "message"), [
    ({"campo": "address", "unidad": "kg"}, "no lleva unidad"),
    ({"campo": "weight_kg", "unidad": "pulgadas"}, "no reconocida"),
    ({"campo": "tw_start", "formato": "hh:mm"}, "formato de fecha"),
    ({"campo": "no_existe"}, "no existe"),
    ({"campo": "weight_kg", "color": "rojo"}, "claves desconocidas"),
    (42, "mapea a un campo"),
])
def test_rechaza_lo_que_no_se_puede_aplicar(value, message):
    with pytest.raises(ColumnSpecError, match=message):
        parse_column_spec(value, TYPES)


def _csv(tmp_path, rows):
    path = tmp_path / "entregas.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    return path


def _values(result):
    return {r.values["delivery_id"]: r.values for r in result.outcome.rows}


def test_convierte_unidades_y_formatos_declarados(tmp_path):
    src = _csv(tmp_path, [
        ["id", "direccion", "lat", "lng", "Weight", "Largo", "Vol", "Desde", "Hasta"],
        ["A1", "Calle 1", "-34.60", "-58.38", "10", "12", "2,5", "09/17/2026 02:30 PM", "09/17/2026 04:00 PM"],
        ["A2", "Calle 2", "-34.61", "-58.39", "1.234,5", "1", "1", "09/18/2026 09:00 AM", "09/18/2026 11:15 AM"],
    ])
    result = run_normalize(src, SCHEMA, tmp_path / "out.csv", manual_mapping={
        "id": "delivery_id", "direccion": "address", "lat": "lat", "lng": "lng",
        "Weight": {"field": "weight_kg", "unit": "pounds", "format": "coma decimal"},
        "Largo": {"campo": "length_cm", "unidad": "pulgadas"},
        "Vol": {"campo": "volume_cm3", "unidade": "litros", "formato": "vírgula decimal"},
        "Desde": {"campo": "tw_start", "formato": "mm/dd/aaaa hh:mm am/pm"},
        "Hasta": {"field": "tw_end", "format": "MM/DD/YYYY hh:mm AM/PM"},
    })
    rows = _values(result)
    assert rows["A1"]["weight_kg"] == pytest.approx(4.535924, rel=1e-6)
    assert rows["A2"]["weight_kg"] == pytest.approx(1234.5 * 0.45359237, rel=1e-6)
    assert rows["A1"]["length_cm"] == pytest.approx(30.48)
    assert rows["A1"]["volume_cm3"] == pytest.approx(2500)
    assert rows["A1"]["tw_start"] == "2026-09-17 14:30"
    assert rows["A2"]["tw_start"] == "2026-09-18 09:00"
    assert rows["A1"]["tw_end"] == "2026-09-17 16:00"
    m = result.mapping.mapping["Weight"]
    assert (m.method, m.unit, m.format) == ("manual", "lb", "decimal_comma")
    assert m.as_dict()["unit"] == "lb"


def test_la_unidad_declarada_gana_sobre_el_nombre_de_la_columna(tmp_path):
    # 'peso lb' se convertia solo; si el usuario dice que son kilos, son kilos.
    src = _csv(tmp_path, [["id", "direccion", "lat", "lng", "peso lb"], ["B1", "Calle", "-34.6", "-58.4", "10"]])
    result = run_normalize(src, SCHEMA, tmp_path / "out.csv", manual_mapping={
        "id": "delivery_id", "direccion": "address", "lat": "lat", "lng": "lng",
        "peso lb": {"campo": "weight_kg", "unidad": "kilos"},
    })
    assert _values(result)["B1"]["weight_kg"] == 10


def test_una_fecha_que_no_cumple_el_formato_queda_vacia_y_avisa(tmp_path):
    src = _csv(tmp_path, [["id", "direccion", "lat", "lng", "Desde"],
                          ["C1", "Calle", "-34.6", "-58.4", "17/09/2026 10:00"]])
    result = run_normalize(src, SCHEMA, tmp_path / "out.csv", manual_mapping={
        "id": "delivery_id", "direccion": "address", "lat": "lat", "lng": "lng",
        "Desde": {"field": "tw_start", "format": "month first"},
    })
    assert _values(result)["C1"].get("tw_start") is None
    assert any("no cumplen el formato" in w for w in result.outcome.warnings)
