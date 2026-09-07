"""Review completo del flujo Smart Input por variante de ingreso (100 archivos).

Cada archivo en `examples/smart-input-variants/` representa un dialecto real
(WhatsApp, email, JSON parcial, speech-to-text, logs, etc.). El test valida:

  1. el pipeline NO tumba el job
  2. se recupera al menos `min_deliveries` detectables
  3. si el manifest pide free_text, el gate lo respeta (TXT)

No exige extraccion perfecta en formatos hostiles: el objetivo es no perder
info detectable ni romper capas (lectura → modo → extract/map → assemble).
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from smart_import.config import Config
from smart_import.pipeline import run_normalize
from tests.conftest import SCHEMA

ROOT = Path(__file__).resolve().parents[1]
VAR_DIR = ROOT / "examples" / "smart-input-variants"
MANIFEST = json.loads((VAR_DIR / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cfg():
    return replace(Config.from_env(), libpostal_enabled=False)


def test_manifest_tiene_100_variantes():
    assert len(MANIFEST) == 100
    files = list(VAR_DIR.glob("v*.*"))
    assert len(files) >= 100


def test_manifest_ids_unicos_y_archivos_existen():
    ids = [m["id"] for m in MANIFEST]
    assert len(ids) == len(set(ids))
    for m in MANIFEST:
        path = VAR_DIR / m["file"]
        assert path.exists(), m["file"]
        assert path.stat().st_size > 20, m["file"]


@pytest.mark.parametrize("entry", MANIFEST, ids=[m["id"] for m in MANIFEST])
def test_smart_input_variant_flow(entry, tmp_path, cfg):
    path = VAR_DIR / entry["file"]
    result = run_normalize(
        path, SCHEMA, tmp_path / f"{entry['id']}.csv",
        emit=("flat", "nested"),
        config=cfg,
        phone_region="AR",
    )
    report = result.report
    assert result.outputs.get("flat"), f"{entry['id']}: sin flat"
    assert "error" not in (report.get("status") or "").lower()
    deliveries = int(report.get("deliveries") or 0)
    min_d = int(entry.get("min_deliveries") or 0)
    allow_empty = bool(entry.get("allow_empty")) or min_d == 0

    if allow_empty and deliveries == 0:
        # Gap documentado: el job completa, free_text gate OK, sin deliveries.
        assert entry.get("gap_note") or "known_gap" in (entry.get("tags") or []), entry["id"]
    else:
        assert deliveries >= max(min_d, 1), (
            f"{entry['id']} ({entry['title']}): deliveries={deliveries} < {min_d}; "
            f"mode={report.get('text_mode')}; notes={report.get('input', {}).get('notes')}"
        )

    expect_mode = entry.get("expect_mode")
    if expect_mode == "free_text" and path.suffix == ".txt":
        mode = report.get("text_mode") or (report.get("input") or {}).get("text_mode")
        assert mode == "free_text", f"{entry['id']}: esperado free_text, got {mode!r}"

    # Sin AI en esta batería (libpostal/AI off en fixture)
    ai_calls = report.get("ai_calls")
    if ai_calls is None:
        ai_calls = (report.get("extraction") or {}).get("ai_calls", 0)
    assert int(ai_calls or 0) == 0


def test_variantes_clave_recuperan_varios_stops(tmp_path, cfg):
    """Spot-check: dialectos claros deben sacar >=2 (mejor review del flujo)."""
    key_ids = {"v01", "v03", "v04", "v08", "v09", "v11", "v14", "v27", "v48", "v96"}
    for entry in MANIFEST:
        if entry["id"] not in key_ids:
            continue
        path = VAR_DIR / entry["file"]
        result = run_normalize(
            path, SCHEMA, tmp_path / f"key_{entry['id']}.csv",
            emit=("nested",), config=cfg, phone_region="AR",
        )
        assert (result.report.get("deliveries") or 0) >= 2, entry["id"]
