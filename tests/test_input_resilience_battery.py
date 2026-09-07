"""Bateria amplia de resiliencia de entrada (>=100 casos).

Objetivo:
  - maximizar entregas recuperadas ante roturas / faltantes / mal tabulado
  - no degradar caminos regression_safe (CSV/JSON/TXT limpios)
  - cada caso es independiente; una capa rota no debe tumbar el resto

Los archivos se materializan en tmp_path (no se versionan).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from smart_import.config import Config
from smart_import.pipeline import run_normalize
from tests.conftest import SCHEMA
from tests.resilience_cases import CATALOG, ResilienceCase


@pytest.fixture(scope="module")
def cfg():
    """Config estable: no depender de .env del developer para la bateria."""
    from dataclasses import replace
    return replace(Config.from_env(), libpostal_enabled=False)


def _run(case: ResilienceCase, tmp_path: Path, cfg: Config):
    path = tmp_path / f"{case.id}{case.suffix}"
    path.write_text(case.build(), encoding="utf-8")
    return run_normalize(
        path, SCHEMA, tmp_path / f"{case.id}_out.csv",
        emit=("flat", "nested"),
        config=cfg,
        phone_region=case.phone_region,
    )


def test_catalog_has_at_least_100_cases():
    assert len(CATALOG) >= 100, f"catalogo insuficiente: {len(CATALOG)}"


def test_catalog_ids_are_unique():
    ids = [c.id for c in CATALOG]
    assert len(ids) == len(set(ids))


def test_catalog_covers_formats():
    suffixes = {c.suffix for c in CATALOG}
    assert {".csv", ".txt", ".json", ".tsv"} <= suffixes


def test_catalog_has_regression_safe_and_broken():
    tags = {t for c in CATALOG for t in c.tags}
    assert "regression_safe" in tags
    assert "broken" in tags or "poison" in tags or "salvage" in tags or "repair" in tags


@pytest.mark.parametrize("case", CATALOG, ids=[c.id for c in CATALOG])
def test_resilience_case(case: ResilienceCase, tmp_path, cfg):
    result = _run(case, tmp_path, cfg)
    report = result.report

    assert result.outputs.get("flat"), f"{case.id}: no emitio flat"
    deliveries = report.get("deliveries") or 0
    packages = report.get("packages") or 0

    if case.exact_deliveries is not None:
        assert deliveries == case.exact_deliveries, (
            f"{case.id}: deliveries={deliveries} expected exact {case.exact_deliveries}; "
            f"notes={report.get('input', {}).get('notes')}"
        )
    else:
        assert deliveries >= case.min_deliveries, (
            f"{case.id}: deliveries={deliveries} < min {case.min_deliveries}; "
            f"notes={report.get('input', {}).get('notes')}"
        )

    if case.min_packages is not None:
        assert packages >= case.min_packages, (
            f"{case.id}: packages={packages} < min {case.min_packages}"
        )

    if case.expect_text_mode is not None:
        mode = report.get("text_mode")
        if mode is None:
            mode = (report.get("input") or {}).get("text_mode")
        assert mode == case.expect_text_mode, (
            f"{case.id}: text_mode={mode!r} expected {case.expect_text_mode!r}"
        )

    notes = " ".join((report.get("input") or {}).get("notes") or [])
    for needle in case.notes_any:
        assert needle.lower() in notes.lower(), (
            f"{case.id}: note {needle!r} no encontrada en {notes!r}"
        )

    if case.must_see or case.must_not_see:
        nested_path = result.outputs.get("nested")
        assert nested_path, f"{case.id}: sin nested para asserts de contenido"
        doc = json.loads(Path(nested_path).read_text(encoding="utf-8"))
        blob = json.dumps(doc, ensure_ascii=False)
        for needle in case.must_see:
            assert needle in blob, f"{case.id}: falta {needle!r} en nested"
        for needle in case.must_not_see:
            assert needle not in blob, f"{case.id}: no debia aparecer {needle!r}"


@pytest.mark.parametrize(
    "case",
    [c for c in CATALOG if "regression_safe" in c.tags],
    ids=[c.id for c in CATALOG if "regression_safe" in c.tags],
)
def test_regression_safe_does_not_need_salvage_notes(case, tmp_path, cfg):
    """Caminos limpios: no deberian disparar salvage/repair agresivo."""
    result = _run(case, tmp_path, cfg)
    notes = " ".join((result.report.get("input") or {}).get("notes") or []).lower()
    if case.suffix == ".json" and "clean" in case.tags:
        assert "salvamento" not in notes
        assert "reparado" not in notes
    if case.suffix == ".csv" and "clean" in case.tags:
        assert result.report["deliveries"] == case.exact_deliveries
        assert result.report.get("invalid_rows", 0) == 0
