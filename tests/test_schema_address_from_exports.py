"""Exports reales → esquema Vepathos: address compuesto y coords de truth.

Los sources viven en examples/geocode-truth/sources/. Las filas sin lat/lng
válidos no entran al corpus de geocode (ver *_12.json).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from smart_import.config import Config
from smart_import.geocoding.accuracy import (
    find_column, find_house_column, has_house_number, load_truth,
)
from smart_import.geocoding.address import parse
from smart_import.mapping import build_mapper
from smart_import.normalization.row_normalizer import RowNormalizer
from smart_import.normalization.values import to_float
from smart_import.readers import read_any
from smart_import.schemas import TargetSchema
from tests.conftest import ROOT

SCHEMA = ROOT / "schemas" / "vepathos_flat_v1.json"
SRC = ROOT / "examples" / "geocode-truth" / "sources"
TRUTH = ROOT / "examples" / "geocode-truth"


def _valid_coord(lat, lng) -> bool:
    la, lo = to_float(lat), to_float(lng)
    return (
        la is not None and lo is not None
        and -90 <= la <= 90 and -180 <= lo <= 180
        and not (abs(la) > 90 or abs(lo) > 180)
        and not (la == 95 and lo == 200)
    )


def test_source_tiendanube_compone_calle_y_numero():
    filas, columnas = load_truth(SRC / "tiendanube-orders-ba.csv")
    assert columnas["address"]
    assert columnas["lat"]
    assert columnas["lng"]
    assert columnas.get("house") in ("Número", "Numero", "número")
    riv = next(f for f in filas if "Rivadavia" in str(f.get(columnas["address"])))
    assert "4800" in str(riv[columnas["address"]])
    valid = [f for f in filas if _valid_coord(f.get(columnas["lat"]), f.get(columnas["lng"]))]
    assert len(valid) >= 12


def test_source_mercadolibre_detecta_address_line():
    filas, columnas = load_truth(SRC / "mercadolibre-shipment-flattened.csv")
    assert columnas["address"] == "address_line"
    assert "latitude" in columnas["lat"]
    valid = [f for f in filas if _valid_coord(f.get(columnas["lat"]), f.get(columnas["lng"]))]
    assert len(valid) == 12
    assert any("Santa Fe 3200" in str(f[columnas["address"]]) for f in valid)


def test_source_vepathos_dedupe_por_address():
    filas, columnas = load_truth(SRC / "vepathos-orders.csv")
    assert columnas["address"] == "address"
    valid = [f for f in filas if _valid_coord(f.get(columnas["lat"]), f.get(columnas["lng"]))]
    addrs = {str(f[columnas["address"]]) for f in valid}
    assert "Maipú 400, Buenos Aires" in addrs
    assert len(addrs) == 12


def test_source_shopify_mapea_address1_y_coords():
    table = read_any(SRC / "shopify-orders-caba.json")
    schema = TargetSchema.load(SCHEMA)
    mapping = build_mapper(Config.from_env()).detect(table, schema)
    outcome = RowNormalizer(schema).run(table, mapping)
    with_addr = [r for r in outcome.rows if r.values.get("address")]
    assert len(with_addr) >= 12
    defensa = next(r for r in with_addr if "Defensa" in (r.values.get("address") or ""))
    assert "800" in defensa.values["address"]
    parsed = parse(defensa.values["address"])
    assert parsed.house_number == "800"
    assert parsed.road and "Defensa" in parsed.road


@pytest.mark.parametrize("name,n,sample", [
    ("tiendanube_ba_12.json", 12, "Rivadavia 4800"),
    ("mercadolibre_ba_12.json", 12, "Santa Fe 3200"),
    ("vepathos_orders_caba_12.json", 12, "Libertador 900"),
    ("shopify_caba_12.json", 12, "Defensa 800"),
    ("miami_whatsapp_6.json", 6, "NE 1st Ave"),
])
def test_truth_corpus_solo_coords_validas(name, n, sample):
    filas, columnas = load_truth(TRUTH / name)
    assert len(filas) == n
    for fila in filas:
        assert _valid_coord(fila.get(columnas["lat"]), fila.get(columnas["lng"]))
        addr = str(fila.get(columnas["address"]) or "")
        assert has_house_number(addr)
        parsed = parse(addr)
        assert parsed.house_number, addr
        assert parsed.road, addr
    assert any(sample.split()[0] in str(f[columnas["address"]]) for f in filas)


def test_truth_tiendanube_no_usa_el_nro_de_orden_como_altura():
    filas, columnas = load_truth(TRUTH / "tiendanube_ba_12.json")
    juan = next(f for f in filas if f.get("customer_name") == "Juan Pérez")
    assert "4800" in str(juan[columnas["address"]])
    assert "1001" not in str(juan[columnas["address"]])
