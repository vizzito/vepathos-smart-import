"""Guardas de DETECT/NORMALIZE que salieron de planillas reales (2026-09-15).

  * `geocode_confidence` (0.8..1.0) se mapeaba a `weight_kg` al regenerar el
    nested: cada entrega geocodificada ganaba un bulto de ~1 kg.
  * Una columna de producto con UNA celda ('Blanco 8kg' = 0) se mapeaba a
    `house_number`; otra con un solo valor ('Garrafas' = 1) a `priority`.
  * El pie de la planilla escrito en la columna de direccion ('totales bolsas 0',
    'kilos cargados') salia como entrega a ubicar a mano.
"""
from __future__ import annotations

import pytest

from smart_import.mapping.heuristics import candidates
from smart_import.normalization.row_normalizer import is_summary_row_text
from smart_import.pipeline import run_normalize
from tests.conftest import ROOT

SCHEMA = ROOT / "schemas" / "vepathos_flat_v1.json"


def test_round_trip_geocodificado_no_convierte_la_confianza_en_peso(tmp_path):
    src = tmp_path / "geocoded.csv"
    src.write_text(
        "address,lat,lng,geocode_status,geocode_confidence,geocode_band\n"
        "Chubut 2020,-37.3066,-59.1742,low_confidence,1.000,review\n"
        "Azucena 1139,-37.3080,-59.1777,low_confidence,0.915,review\n"
        "Iraola 1327,-37.3107,-59.1788,matched,0.877,valid\n"
        "Formosa 1858,-37.3099,-59.1745,low_confidence,0.905,review\n",
        encoding="utf-8")
    result = run_normalize(src, SCHEMA, tmp_path / "out.csv", emit=("nested",),
                           expand_composite=False)
    targets = {m.target for m in result.mapping.mapping.values()}
    assert "weight_kg" not in targets
    assert not any(col.startswith("geocode_") for col in result.mapping.mapping)
    assert all(not d.get("packages") for d in result.deliveries)


@pytest.mark.parametrize("values,target", [
    ([0], "house_number"),          # 'Blanco 8kg': una celda con 0
    ([1], "priority"),              # 'Garrafas': una celda con 1
    ([3, 7], "priority"),           # dos celdas tampoco son una columna
])
def test_una_columna_casi_vacia_no_tiene_forma_de_altura_ni_prioridad(values, target):
    assert target not in {t for t, *_ in candidates(values)}


def test_una_columna_de_alturas_de_verdad_sigue_detectandose():
    assert "house_number" in {t for t, *_ in candidates([1219, 162, 2020, 471, 1857])}


@pytest.mark.parametrize("text", [
    "totales bolsas 0", "kilos cargados", "Total 1195", "TOTAL GENERAL",
    "subtotal: 450", "sum 23 boxes",
])
def test_pie_de_planilla(text):
    assert is_summary_row_text(text)


@pytest.mark.parametrize("text", [
    "Totoral 1195", "Suma Paz 123", "Total Petroleum 45", "Av. Total 123",
    "Moreno 245", "",
])
def test_una_direccion_no_es_pie_de_planilla(text):
    assert not is_summary_row_text(text)


def test_normalize_ignora_el_pie_sin_contarlo_como_pendiente(tmp_path):
    src = tmp_path / "planilla.csv"
    src.write_text(
        "direccion,bultos\n"
        "Roca 1160,2\n"
        "Avellaneda 1725,1\n"
        "totales bolsas 0,3\n"
        "kilos cargados,39\n",
        encoding="utf-8")
    result = run_normalize(src, SCHEMA, tmp_path / "out.csv")
    counts = result.outcome.counts()
    assert counts["needs_geocode"] == 2
    assert counts["ignored"] == 2


def test_m3_y_moneda_se_convierten_a_las_unidades_del_schema(tmp_path):
    """'m3' (0.02) entraba como volume_cm3=0.02 y 'valor' (15000 pesos) como
    value_cents=15000 ($150). El round-trip (headers del schema) no reconvierte."""
    src = tmp_path / "export.csv"
    src.write_text("order,direccion,m3,valor\n"
                   "A1,Corrientes 1234,0.02,15000\n"
                   "A2,Cabildo 2400,0.15,23000\n"
                   "A3,Santa Fe 3200,0.08,9800\n", encoding="utf-8")
    first = run_normalize(src, SCHEMA, tmp_path / "flat.csv", emit=("flat",))
    pkg = first.deliveries[0]["packages"][0]
    assert pkg["volume_cm3"] == 20000.0
    assert pkg["value_cents"] == 1_500_000
    again = run_normalize(tmp_path / "flat.csv", SCHEMA, tmp_path / "rt.csv",
                          emit=("nested",), expand_composite=False)
    assert again.deliveries[0]["packages"][0] == pkg


@pytest.mark.parametrize("header,m3,major", [
    ("m3", True, None), ("CBM", True, None), ("volume_cm3", False, None),
    ("Volumen (cm3)", False, None), ("valor", None, True), ("value_cents", None, False),
    ("importe centavos", None, False),
])
def test_headers_que_declaran_unidad(header, m3, major):
    from smart_import.normalization.units import (
        column_declares_cubic_meters, column_declares_major_currency,
    )
    if m3 is not None:
        assert column_declares_cubic_meters(header) is m3
    if major is not None:
        assert column_declares_major_currency(header) is major
