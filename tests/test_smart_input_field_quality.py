"""Calidad por campo en variantes Smart Input (goldens flexibles).

No exige string exacto: matchea por telefono (digitos) y substrings de
nombre/direccion. Si el conteo baja o se pierde un campo clave, falla.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from smart_import.config import Config
from smart_import.pipeline import run_normalize
from tests.conftest import SCHEMA

ROOT = Path(__file__).resolve().parents[1]
VAR_DIR = ROOT / "examples" / "smart-input-variants"
GOLDEN_DIR = VAR_DIR / "expected"
MANIFEST = {m["id"]: m for m in json.loads((VAR_DIR / "manifest.json").read_text())}


@pytest.fixture(scope="module")
def cfg():
    return replace(Config.from_env(), libpostal_enabled=False)


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold().strip())


def _find_stop(stops: list[dict], expect: dict) -> dict | None:
    """Busca un stop por telefono (preferido) o por overlap de address/name."""
    phone_digits = _digits(expect.get("phone_contains") or expect.get("phone") or "")
    if phone_digits:
        for s in stops:
            if phone_digits in _digits(s.get("phone")):
                return s
    addr_need = [ _norm(x) for x in (expect.get("address_contains") or []) ]
    name_need = _norm(expect.get("customer_name") or expect.get("customer_name_contains") or "")
    for s in stops:
        addr = _norm(s.get("address"))
        name = _norm(s.get("customer_name"))
        if addr_need and all(a in addr for a in addr_need):
            if not name_need or name_need in name:
                return s
        if name_need and name_need in name and not addr_need:
            return s
    return None


def _load_goldens() -> list[Path]:
    return sorted(GOLDEN_DIR.glob("v*.expected.json"))


@pytest.mark.parametrize("golden_path", _load_goldens(), ids=lambda p: p.stem.split(".")[0])
def test_field_quality_golden(golden_path: Path, tmp_path, cfg):
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    vid = golden["id"]
    entry = MANIFEST[vid]
    result = run_normalize(
        VAR_DIR / entry["file"], SCHEMA, tmp_path / f"{vid}.csv",
        emit=("nested",), config=cfg, phone_region="AR",
    )
    stops = json.loads(Path(result.outputs["nested"]).read_text())["addresses"]
    assert len(stops) >= int(golden.get("min_deliveries") or 1), (
        f"{vid}: deliveries={len(stops)} < {golden.get('min_deliveries')}"
    )

    unmatched = []
    for expect in golden.get("stops") or []:
        hit = _find_stop(stops, expect)
        if hit is None:
            unmatched.append(expect)
            continue
        if "customer_name" in expect and expect["customer_name"]:
            assert _norm(expect["customer_name"]) in _norm(hit.get("customer_name")), (
                f"{vid}: name want {expect['customer_name']!r} got {hit.get('customer_name')!r}"
            )
        if "customer_name_contains" in expect:
            assert _norm(expect["customer_name_contains"]) in _norm(hit.get("customer_name")), (
                f"{vid}: name~{expect['customer_name_contains']!r} got {hit.get('customer_name')!r}"
            )
        for needle in expect.get("address_contains") or []:
            assert _norm(needle) in _norm(hit.get("address")), (
                f"{vid}: address missing {needle!r} in {hit.get('address')!r}"
            )
        house_needles = [n for n in (expect.get("address_contains") or [])
                         if str(n).isdigit()]
        if house_needles and hit.get("address"):
            from smart_import.geocoding.address import parse
            parsed = parse(hit["address"])
            got = (parsed.house_number or "").lstrip("0") or parsed.house_number
            for n in house_needles:
                assert n.lstrip("0") in (got or "").lstrip("0") or n in (hit["address"] or ""), (
                    f"{vid}: house {n!r} not parsed from {hit.get('address')!r} ({parsed.house_number!r})"
                )
        if expect.get("phone_contains") or expect.get("phone"):
            want = _digits(expect.get("phone_contains") or expect.get("phone"))
            assert want in _digits(hit.get("phone")), (
                f"{vid}: phone want~{want} got {hit.get('phone')!r}"
            )
        if expect.get("require_phone"):
            assert hit.get("phone"), f"{vid}: expected phone on {expect}"

    assert not unmatched, f"{vid}: stops no matcheados: {unmatched}"
