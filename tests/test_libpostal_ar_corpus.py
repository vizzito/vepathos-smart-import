"""AR corpora: con vs sin SMART_IMPORT_LIBPOSTAL_ENABLED.

LatAm tipico NO deberia beneficiarse de libpostal. Estos tests miden:

  * misma cantidad de entregas / mismos campos clave con flag off y on
  * con libreria instalada: casi todo son skips; added ≈ 0
  * sin libreria: flag on degrada a heuristic (mismo resultado que off)

Correr:

    pytest -q tests/test_libpostal_ar_corpus.py -s
    pytest -q tests/test_libpostal_ar_corpus.py -m libpostal -s   # exige postal
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from smart_import.addresses import EnhancingAddressParser, is_installed
from smart_import.config import Config
from smart_import.extraction.context import ExtractionContext
from smart_import.extraction.free_text import FreeTextExtractor
from tests.conftest import FREE_TEXT

DAY = date(2026, 9, 6)

CORPORA = (
    ("paste_55_caba", FREE_TEXT / "paste_55_caba.txt", 55, 4),
    ("whatsapp_12", FREE_TEXT / "whatsapp_12.txt", 12, 3),
)


def _snapshot(document: str, *, libpostal_enabled: bool) -> dict:
    cfg = Config.from_env().replace(libpostal_enabled=libpostal_enabled)
    ctx = ExtractionContext.from_config(cfg, phone_region="AR")
    extractor = FreeTextExtractor(cfg, ctx, service_date=DAY)
    result = extractor.run_document(document)
    parser = extractor.fields.address_parser
    stats = parser.stats() if isinstance(parser, EnhancingAddressParser) else {
        "enhancer_calls": 0, "enhancer_skips": 0,
        "fields_added": 0, "fields_rejected": 0,
    }
    rows = []
    for record in result.records:
        rows.append({
            "name": record.get("customer_name") or "",
            "address": record.get("address") or "",
            "phone": record.get("phone") or "",
            "tw_end": record.get("tw_end") or "",
        })
    rows.sort(key=lambda r: (r["phone"], r["address"]))
    return {
        "parser": parser.name,
        "deliveries": len(result.records),
        "ignored": len(result.ignored),
        "rows": rows,
        "stats": stats,
    }


def _print_compare(label: str, off: dict, on: dict) -> None:
    s_off, s_on = off["stats"], on["stats"]
    print(f"\n=== {label} ===")
    print(f"  off: parser={off['parser']} entregas={off['deliveries']} "
          f"ignorados={off['ignored']}")
    print(f"  on:  parser={on['parser']} entregas={on['deliveries']} "
          f"ignorados={on['ignored']}  "
          f"calls={s_on['enhancer_calls']} skips={s_on['enhancer_skips']} "
          f"added={s_on['fields_added']} rejected={s_on['fields_rejected']}")
    same = off["rows"] == on["rows"]
    print(f"  mismos campos clave: {same}")


@pytest.mark.parametrize("stem,path,n_deliveries,n_ignored", CORPORA,
                         ids=[c[0] for c in CORPORA])
def test_sin_libpostal_extrae_todas_las_entregas(stem, path: Path,
                                                  n_deliveries, n_ignored):
    document = path.read_text(encoding="utf-8")
    off = _snapshot(document, libpostal_enabled=False)
    assert off["parser"] == "heuristic"
    assert off["deliveries"] == n_deliveries
    assert off["ignored"] == n_ignored
    # Todas las entregas traen direccion y telefono
    for row in off["rows"]:
        assert row["address"], row
        assert row["phone"], row


@pytest.mark.parametrize("stem,path,n_deliveries,n_ignored", CORPORA,
                         ids=[c[0] for c in CORPORA])
def test_flag_on_sin_regresion_aunque_falte_la_libreria(
        stem, path: Path, n_deliveries, n_ignored, monkeypatch):
    """Flag true + postal ausente = mismo resultado que flag false."""
    from smart_import.addresses import factory as factory_mod
    monkeypatch.setattr(factory_mod, "is_installed", lambda: False)

    document = path.read_text(encoding="utf-8")
    off = _snapshot(document, libpostal_enabled=False)
    on = _snapshot(document, libpostal_enabled=True)
    assert on["parser"] == "heuristic"
    assert on["deliveries"] == off["deliveries"] == n_deliveries
    assert on["rows"] == off["rows"]


@pytest.mark.libpostal
@pytest.mark.skipif(not is_installed(), reason="libpostal/postal no instalado")
@pytest.mark.parametrize("stem,path,n_deliveries,n_ignored", CORPORA,
                         ids=[c[0] for c in CORPORA])
def test_con_libpostal_mismo_resultado_que_sin_en_AR(
        stem, path: Path, n_deliveries, n_ignored, capsys):
    """En LatAm tipico libpostal no debe cambiar el extract ni aportar campos."""
    document = path.read_text(encoding="utf-8")
    off = _snapshot(document, libpostal_enabled=False)
    on = _snapshot(document, libpostal_enabled=True)
    _print_compare(stem, off, on)

    assert off["parser"] == "heuristic"
    assert on["parser"] == "enhanced"
    assert on["deliveries"] == off["deliveries"] == n_deliveries
    assert on["ignored"] == off["ignored"] == n_ignored
    assert on["rows"] == off["rows"], (
        "libpostal cambio nombre/direccion/telefono/horario en un corpus AR")

    stats = on["stats"]
    # Prioridad heuristica: la mayoria son skips
    assert stats["enhancer_skips"] >= stats["enhancer_calls"]
    # No aporta calidad en estos corpus (added=0); puede rechazar basura
    assert stats["fields_added"] == 0
    # calls puede ser >0 (fragmentos tipo 'Julia Rios, tel') pero sin valor
    total = stats["enhancer_calls"] + stats["enhancer_skips"]
    assert total > 0
    call_rate = stats["enhancer_calls"] / total
    assert call_rate < 0.25, (
        f"demasiadas consultas libpostal en AR: {call_rate:.0%} "
        f"(calls={stats['enhancer_calls']} skips={stats['enhancer_skips']})")
