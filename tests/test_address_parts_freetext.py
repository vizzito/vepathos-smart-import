"""Free-text → partes del schema → compose."""
from __future__ import annotations

from smart_import.addresses import HeuristicAddressParser
from smart_import.config import Config
from smart_import.extraction.address_parts import fill_address_parts_from_text
from smart_import.extraction.free_text import FreeTextExtractor
from smart_import.extraction.result import ExtractedRecord, FieldValue


def test_fill_parts_graduados_tandil():
    record = ExtractedRecord(source="x")
    record.set(FieldValue(
        "address", "graduados 3165, barrio graduados, tandil 7000, argentina",
        "", 0.8, "test"))
    assert fill_address_parts_from_text(record, HeuristicAddressParser())
    assert record.get("house_number") == "3165"
    assert "graduados" in (record.get("address") or "").lower()
    assert "3165" in (record.get("address") or "")
    # localidad / CP si el parser los vio
    assert record.get("postcode") in (None, "7000") or record.get("postcode") == "7000"
    flat_city = (record.get("city") or record.get("zone") or "").lower()
    assert "tandil" in flat_city or "tandil" in (record.get("address") or "").lower()


def test_free_text_extractor_llena_partes():
    cfg = Config.from_env().replace(default_phone_region="AR")
    ext = FreeTextExtractor(cfg)
    doc = ext.run_document(
        "Ana Perez — Cordoba 444, Tandil, barrio terminal — 11 4002-1002"
    )
    assert doc.records, doc.as_dict()
    rec = doc.records[0]
    assert rec.get("house_number") == "444" or "444" in (rec.get("address") or "")
    assert "444" in (rec.get("address") or "")
    assert rec.get("phone")


def test_fill_no_pisa_etiqueta_previa():
    record = ExtractedRecord(source="x")
    record.set(FieldValue("address", "Av. Corrientes 100, CABA", "", 0.9, "test"))
    record.set(FieldValue("city", "Ya Mapeada", "", 1.0, "label"))
    fill_address_parts_from_text(record, HeuristicAddressParser())
    assert record.get("city") == "Ya Mapeada"
