"""Valor real de libpostal: heuristic vs libpostal vs enhanced (con/sin flag).

Requiere la libreria C + binding Python:

    brew install libpostal
    pip install -e ".[libpostal]"

Correr (imprime la tabla de deltas):

    pytest -q tests/test_libpostal_value.py -m libpostal -s

Sin libpostal instalado estos tests se SALTEAN (el resto del suite sigue verde).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from smart_import.addresses import (
    EnhancingAddressParser, HeuristicAddressParser, LibpostalAddressParser,
    build_address_parser, enhancement_reason, is_installed, needs_enhancement,
)
from smart_import.config import Config
from smart_import.resources import fold

pytestmark = [
    pytest.mark.libpostal,
    pytest.mark.skipif(not is_installed(),
                       reason="libpostal no instalado (brew install libpostal && "
                              "pip install -e '.[libpostal]')"),
]

GOLD_PATH = Path(__file__).parent / "data" / "libpostal_gold.json"

#: campos que pesan para decidir si aporta valor
SCORE_FIELDS = ("road", "house_number", "unit", "postcode", "suburb",
                "city", "landmark", "level", "building")


def _load_cases() -> list[dict]:
    payload = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    return list(payload["cases"])


def _hit(got: str, want: str) -> bool:
    """Match laxo: el gold es un ancla, no igualdad exacta.

    '1st Ave' cumple gold '1st'; 'plot no. 42' cumple gold '42'.
    """
    if not want:
        return True
    a, b = fold(got or ""), fold(want)
    if not a:
        return False
    return b in a or a in b


def score_parsed(components: dict, gold: dict) -> tuple[int, int, list[str]]:
    """(hits, total, misses). Solo cuenta campos presentes en gold."""
    hits = 0
    misses: list[str] = []
    for field, want in gold.items():
        got = components.get(field, "")
        if _hit(got, want):
            hits += 1
        else:
            misses.append(f"{field}: got={got!r} want~{want!r}")
    return hits, len(gold), misses


def _parsers():
    heuristic = HeuristicAddressParser()
    libpostal = LibpostalAddressParser()
    assert libpostal.available()
    enhanced = EnhancingAddressParser(heuristic, libpostal)
    return heuristic, libpostal, enhanced


# ---------- instalacion y flag ----------

def test_libpostal_esta_instalado_y_parsea():
    parser = LibpostalAddressParser()
    assert parser.available()
    parsed = parser.parse("Av. Corrientes 100, CABA")
    assert parsed.get("road")
    assert parsed.get("house_number") == "100"


def test_flag_false_ignora_libpostal_aunque_este_instalado():
    cfg = Config.from_env().replace(libpostal_enabled=False)
    parser = build_address_parser(cfg)
    assert parser.name == "heuristic"
    # Caso que ABRIRIA el gate: sigue siendo heuristico puro
    text = "Plot No. 42, Sector 18, Noida 201301"
    assert needs_enhancement(HeuristicAddressParser().parse(text))
    assert parser.parse(text).parser == "heuristic"


def test_flag_true_arma_enhancer():
    cfg = Config.from_env().replace(libpostal_enabled=True, address_parser="enhanced")
    parser = build_address_parser(cfg)
    assert parser.name == "enhanced"
    assert isinstance(parser, EnhancingAddressParser)


# ---------- gate + no-regresion ----------

@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_gate_coincide_con_el_gold(case):
    parsed = HeuristicAddressParser().parse(case["text"])
    assert needs_enhancement(parsed) is case["expect_gate"], (
        f"{case['id']}: reason={enhancement_reason(parsed)} "
        f"road={parsed.get('road')!r}")


@pytest.mark.parametrize("case",
                         [c for c in _load_cases() if not c["expect_gate"] and c["gold"]],
                         ids=lambda c: c["id"])
def test_sin_gate_enhanced_igual_que_heuristico(case):
    """Cuando el gate esta cerrado, enhanced NO debe alterar el resultado."""
    heuristic, libpostal, _ = _parsers()
    enhanced = EnhancingAddressParser(heuristic, libpostal)
    h = heuristic.parse(case["text"])
    e = enhanced.parse(case["text"])
    assert enhanced.enhancer_calls == 0
    assert e.parser == "heuristic"
    assert e.components == h.components


@pytest.mark.parametrize("case",
                         [c for c in _load_cases() if c["expect_gate"]],
                         ids=lambda c: c["id"])
def test_con_gate_enhanced_consulta_libpostal(case):
    heuristic, _lib, enhanced = _parsers()
    before = enhanced.enhancer_calls
    e = enhanced.parse(case["text"])
    assert enhanced.enhancer_calls == before + 1
    assert e.parser == "enhanced"
    assert any("enhancer consultado" in x for x in e.evidence)


# ---------- medicion de valor ----------

def test_tabla_de_valor_heuristic_vs_libpostal_vs_enhanced(capsys):
    """Compara accuracy de campos gold. Imprime tabla (correr con -s).

    Criterios medibles (no opinan, cuentan):
      * no_gate_regression: en casos expect_gate=false, enhanced == heuristic
        en score de gold
      * gate_delta: en casos expect_gate=true, enhanced_hits - heuristic_hits
      * libpostal_alone: score de libpostal puro (referencia)
    """
    cases = _load_cases()
    heuristic, libpostal, enhanced = _parsers()

    rows = []
    totals = {
        "heuristic": [0, 0], "libpostal": [0, 0], "enhanced": [0, 0],
        "gate_n": 0, "gate_improved": 0, "gate_regressed": 0, "gate_same": 0,
        "nongate_regressed": 0,
    }

    for case in cases:
        gold = case["gold"]
        h = heuristic.parse(case["text"])
        l = libpostal.parse(case["text"])
        e = enhanced.parse(case["text"])

        h_hits, n, _ = score_parsed(h.components, gold) if gold else (0, 0, [])
        l_hits, _, _ = score_parsed(l.components, gold) if gold else (0, 0, [])
        e_hits, _, _ = score_parsed(e.components, gold) if gold else (0, 0, [])

        # Contaminacion: campos que libpostal NO debe meter
        for field, banned in (case.get("must_not") or {}).items():
            got = fold(e.get(field) or "")
            assert fold(banned) not in got, (
                f"{case['id']}: enhanced contamino {field}={e.get(field)!r}")

        for key, hits in (("heuristic", h_hits), ("libpostal", l_hits),
                          ("enhanced", e_hits)):
            totals[key][0] += hits
            totals[key][1] += n

        delta = e_hits - h_hits
        if case["expect_gate"]:
            totals["gate_n"] += 1
            if delta > 0:
                totals["gate_improved"] += 1
            elif delta < 0:
                totals["gate_regressed"] += 1
            else:
                totals["gate_same"] += 1
        elif gold and e_hits < h_hits:
            totals["nongate_regressed"] += 1

        rows.append({
            "id": case["id"], "region": case["region"], "gate": case["expect_gate"],
            "h": h_hits, "l": l_hits, "e": e_hits, "n": n, "delta": delta,
            "h_road": h.get("road"), "e_road": e.get("road"), "l_comp": dict(l.components),
        })

    # ---- tabla ----
    print("\n=== libpostal value report ===")
    print(f"{'id':22} {'reg':3} {'gate':5} {'H':>3} {'L':>3} {'E':>3} {'Δ':>3}  road H → E")
    for r in rows:
        print(f"{r['id'][:22]:22} {r['region']:3} {str(r['gate']):5} "
              f"{r['h']:>3} {r['l']:>3} {r['e']:>3} {r['delta']:>+3}  "
              f"{(r['h_road'] or '—')[:18]:18} → {(r['e_road'] or '—')[:18]}")

    def pct(pair):
        hits, total = pair
        return 100.0 * hits / total if total else 0.0

    print("\nAccuracy (campos gold):")
    for name in ("heuristic", "libpostal", "enhanced"):
        hits, total = totals[name]
        print(f"  {name:12} {hits}/{total} = {pct(totals[name]):.1f}%")

    print(f"\nCasos con gate abierto: {totals['gate_n']}")
    print(f"  improved={totals['gate_improved']}  "
          f"same={totals['gate_same']}  regressed={totals['gate_regressed']}")
    print(f"Regresiones fuera de gate: {totals['nongate_regressed']}")

    # Dump detalle libpostal en casos gated (para inspeccion humana)
    print("\nlibpostal raw (solo gate=true):")
    for r in rows:
        if r["gate"]:
            print(f"  {r['id']}: {r['l_comp']}")

    # ---- asserts de decision ----
    # 1) Nunca regresar fuera del gate
    assert totals["nongate_regressed"] == 0, (
        "enhanced empeoro casos donde el gate estaba cerrado")

    # 2) Accuracy enhanced >= heuristic en el corpus completo
    assert pct(totals["enhanced"]) + 1e-9 >= pct(totals["heuristic"]), (
        f"enhanced {pct(totals['enhanced']):.1f}% < "
        f"heuristic {pct(totals['heuristic']):.1f}%")

    # 3) Reportar si aporta valor real en el gate (no falla el suite si es 0:
    #    eso ES el dato para decidir no adoptar). Se marca con warning.
    if totals["gate_n"] and totals["gate_improved"] == 0:
        print("\n⚠️  libpostal NO mejoro ningun caso gated contra el gold. "
              "Evidencia en contra de adoptarlo.")


def test_factory_con_flag_true_mejora_o_igual_en_corpus():
    """Camino de produccion: Config.libpostal_enabled=True."""
    on = build_address_parser(Config.from_env().replace(libpostal_enabled=True))
    off = build_address_parser(Config.from_env().replace(libpostal_enabled=False))
    assert on.name == "enhanced"
    assert off.name == "heuristic"

    on_hits = off_hits = total = 0
    for case in _load_cases():
        gold = case["gold"]
        if not gold:
            continue
        h, n, _ = score_parsed(off.parse(case["text"]).components, gold)
        e, _, _ = score_parsed(on.parse(case["text"]).components, gold)
        off_hits += h
        on_hits += e
        total += n
    assert total > 0
    assert on_hits >= off_hits
