"""Al expandir una columna mapeada a address, el destino no se puede perder."""
from __future__ import annotations

import json
from pathlib import Path

from smart_import.config import Config
from smart_import.extraction.context import ExtractionContext
from smart_import.extraction.free_text import FreeTextExtractor, expand_free_text_column
from smart_import.extraction.result import ExtractedRecord, FieldValue
from smart_import.pipeline import run_normalize
from smart_import.readers.base import FileMeta, Table
from smart_import.schemas import TargetSchema

SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "vepathos_flat_v1.json"

FULL_ADDRESS = (
    "Domingo F. Sarmiento 1219, B7000 Tandil, Provincia de Buenos Aires, Argentina"
)


class _NoAddressExtractor:
    """Extractor que parte campos pero no devolvio address."""

    def run_value(self, text, position: int = 1):
        record = ExtractedRecord(source=str(text or ""))
        record.set(FieldValue("city", "Provincia de Buenos Aires", "", 0.9, "dummy"))
        record.set(FieldValue("house_number", "1219", "", 0.9, "dummy"))
        return record


def test_expand_si_el_origen_se_llama_como_el_campo_extraido_no_lo_tira():
    """Regresion: `name != column` descartaba el campo extraido homonimo.

    Pasa con cualquier columna ya nombrada como el schema (`address`, etc.),
    no solo con un JSON de una ciudad.
    """
    schema = TargetSchema.load(SCHEMA)
    cfg = Config.from_env()
    extractor = FreeTextExtractor(cfg, ExtractionContext.from_config(cfg, phone_region="AR"))
    table = Table(
        meta=FileMeta(path="x.json", format="json"),
        columns=["delivery_id", "address", "zone"],
        rows=[("DLV-1", FULL_ADDRESS, "TANDIL")],
    )
    expanded = expand_free_text_column(table, "address", extractor, schema)
    row = dict(zip(expanded.columns, expanded.rows[0]))
    assert row["address"]
    assert "Sarmiento" in row["address"]
    assert expanded.columns.count("zone") == 1
    assert row["zone"] == "TANDIL"


def test_expand_si_el_extractor_no_trae_address_usa_la_celda_original():
    schema = TargetSchema.load(SCHEMA)
    table = Table(
        meta=FileMeta(path="x.json", format="json"),
        columns=["delivery_id", "address", "zone"],
        rows=[("DLV-1", FULL_ADDRESS, "TANDIL")],
    )
    expanded = expand_free_text_column(table, "address", _NoAddressExtractor(), schema)
    row = dict(zip(expanded.columns, expanded.rows[0]))
    assert row["address"] == FULL_ADDRESS
    assert row["zone"] == "TANDIL"


def test_expand_columna_con_otro_nombre_sigue_separando_campos():
    """Legacy: 'Datos entrega' → address + name + phone; no se pierde el destino."""
    schema = TargetSchema.load(SCHEMA)
    cfg = Config.from_env()
    extractor = FreeTextExtractor(cfg, ExtractionContext.from_config(cfg, phone_region="AR"))
    blob = "Ana Perez - Corrientes 1250, CABA - tel 11 4002-1002"
    table = Table(
        meta=FileMeta(path="x.csv", format="csv"),
        columns=["Datos entrega", "Zona"],
        rows=[(blob, "CABA")],
    )
    expanded = expand_free_text_column(table, "Datos entrega", extractor, schema)
    row = dict(zip(expanded.columns, expanded.rows[0]))
    assert "Datos entrega" not in expanded.columns
    assert "address" in expanded.columns
    assert "Corrientes" in (row.get("address") or "")
    assert row["Zona"] == "CABA"


def test_json_address_largo_sin_coords_queda_para_geocode(tmp_path):
    payload = {
        "addresses": [
            {
                "delivery_id": "DLV-00001",
                "address": FULL_ADDRESS,
                "zone": "TANDIL",
                "packages": [{"package_id": "PKG-1", "weight_kg": 1.2}],
            }
        ]
    }
    path = tmp_path / "sin_coords.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = run_normalize(
        path, SCHEMA, tmp_path / "out.csv",
        emit=("flat",), config=Config.from_env(), phone_region="AR",
    )
    assert result.report["deliveries"] == 1
    assert result.report["invalid_rows"] == 0
    assert result.report["needs_geocode"] == 1
    addr = result.outcome.rows[0].values.get("address") or ""
    assert "Sarmiento" in addr and "1219" in addr
