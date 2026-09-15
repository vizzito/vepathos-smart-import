"""El arnes de regresion: la nota de cada fila y el diff entre snapshots."""
from __future__ import annotations

import json

import pytest

from smart_import.tools.geocode_regression import (
    GRADES, Expectation, diff_snapshots, grade_row, load_cases, summarize,
)


def _point(lat=-34.6, lng=-58.4):
    return Expectation(kind="point", lat=lat, lng=lng)


@pytest.mark.parametrize("band,offset_m,outcome", [
    ("valid", 20, "green_good"),
    ("review", 20, "amber_good"),
    ("review", 300, "amber_fair"),
    ("valid", 300, "green_fair"),
    ("review", 3000, "amber_bad"),
    ("valid", 3000, "green_bad"),
])
def test_nota_por_banda_y_distancia(band, offset_m, outcome):
    pin = (-34.6 + offset_m / 111_000, -58.4)
    got, dist = grade_row(_point(), band, pin, good_m=100, fair_m=500)
    assert got == outcome
    assert dist == pytest.approx(offset_m, rel=0.02)


def test_un_verde_lejos_es_peor_que_no_tener_pin():
    assert GRADES["green_bad"] < GRADES["amber_bad"] < GRADES["none"] < GRADES["amber_fair"]
    assert GRADES["green_fair"] < GRADES["none"]


def test_basura_sin_pin_es_el_resultado_correcto():
    none = Expectation(kind="none")
    assert grade_row(none, "needs_geocoding", None, good_m=100, fair_m=500)[0] == "none_expected"
    assert grade_row(none, "review", (-34.6, -58.4), good_m=100, fair_m=500)[0] == "amber_bad"


def _row(key, grade_outcome, band="valid", pin=None, dist=None):
    return {"key": key, "suite": "s", "address": key, "outcome": grade_outcome,
            "grade": GRADES[grade_outcome], "band": band, "pin": pin, "dist_m": dist}


def test_diff_separa_regresiones_mejoras_y_verde_a_ambar_con_el_mismo_pin():
    before = {
        "a": _row("a", "none", band="needs_geocoding"),
        "b": _row("b", "green_good", pin=[-34.6, -58.4], dist=10),
        "c": _row("c", "green_bad", pin=[-34.6, -58.4], dist=900),
        "d": _row("d", "amber_good", band="review", pin=[-34.6, -58.4], dist=10),
    }
    after = {
        "a": _row("a", "amber_good", band="review", pin=[-34.6, -58.4], dist=10),
        "b": _row("b", "amber_good", band="review", pin=[-34.6, -58.4], dist=10),
        "c": _row("c", "amber_bad", band="review", pin=[-34.6, -58.4], dist=900),
        "d": _row("d", "none", band="needs_geocoding"),
    }
    kinds = {c.key: c.kind for c in diff_snapshots(before, after)["changes"]}
    assert kinds == {"a": "improvement", "b": "demoted", "c": "improvement",
                     "d": "regression"}


def test_el_corpus_no_puede_cambiar_entre_snapshots():
    before = {"a": _row("a", "none", band="needs_geocoding")}
    after = {"a": {**_row("a", "none", band="needs_geocoding"), "address": "otra"}}
    with pytest.raises(ValueError):
        diff_snapshots(before, after)


def test_carga_expectativas_de_calle_esquina_y_basura(tmp_path):
    corpus = tmp_path / "c.json"
    corpus.write_text(json.dumps({"rows": [
        {"address": "alvarado 471", "expect": {"kind": "street", "streets": ["General Rudecindo Alvarado"]}},
        {"address": "lungui y navarro", "expect": {"kind": "intersection", "lat": -37.29, "lng": -59.17}},
        {"address": "Gonzalo", "expect": {"kind": "none"}},
        {"address": "Roca 162", "lat": -37.32, "lng": -59.12},
        {"address": "sin verdad"},
    ]}), encoding="utf-8")
    cases = load_cases(corpus)
    assert [c.expect.kind for c in cases] == ["street", "intersection", "none", "point"]


def test_resumen_cuenta_falsos_verdes():
    rows = [_row("a", "green_bad", pin=[0, 0], dist=900), _row("b", "green_good", pin=[0, 0], dist=5)]
    s = summarize(rows)
    assert s["false_green"] == 1 and s["green_good"] == 1
