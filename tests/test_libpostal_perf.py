"""Performance: 500 direcciones, con vs sin libpostal.

Responde dos preguntas de decision:

  1. Si el gate NO llama libpostal, ¿igual paga RAM/CPU?
  2. Con un corpus mixto (LatAm + US + India + Asia), ¿cuanto cuesta el enhancer?

Cada escenario corre en un **subprocess** limpio: una vez que `import postal`
carga los ~2 GB de datos, el RSS no baja en el mismo proceso.

Correr:

    pytest -q tests/test_libpostal_perf.py -m libpostal -s

Sin libpostal instalado → skip.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from smart_import.addresses.libpostal_parser import is_installed

pytestmark = [
    pytest.mark.libpostal,
    pytest.mark.skipif(
        not is_installed(),
        reason="libpostal no instalado (brew install libpostal && pip install -e '.[libpostal]')",
    ),
]

N = 500
ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Corpus sintetico (~500). Mixto a proposito: gate cerrado + gate abierto.
# ---------------------------------------------------------------------------

_LATAM = [
    "Av. Corrientes {n}, CABA",
    "Malabia {n}, Palermo",
    "Av. Santa Fe {n}, Buenos Aires",
    "Calle 50 nro {n}, Medellin",
    "Carrera 7 # {n}, Bogota",
    "Av. Paulista, {n}, Sao Paulo",
    "Rua Augusta, {n}, Sao Paulo",
    "Av. Insurgentes Sur {n}, Ciudad de Mexico",
    "Av. Providencia {n}, Santiago",
    "Calle Gran Via {n}, Madrid",
    "Av. Pueyrredon {n}, Recoleta",
    "11 de Septiembre Nro {n}",
]

_US_UK = [
    "{n} 1st Ave, Seattle",
    "{n} 5th Avenue, New York",
    "{n} Yesler Way, Seattle",
    "Apt 4B, {n} 5th Ave, New York",
    "{n} Oxford Street, London",
    "{n} King Street, Toronto",
]

_GATE_OPEN = [
    "Flat {n}B, Shanti Nagar, Near Hanuman Temple, Andheri East, Mumbai 400069",
    "Plot No. {n}, Sector 18, Noida 201301",
    "{n}th Floor, Cyber Towers, Hitech City, Hyderabad 500081",
    "Manzana {n} Casa 5, Barrio Norte",
    "Lote {n}, Manzana 12, Quilmes",
    "Villa {n}, Al Barsha, Dubai",
    "Building {n}, Al Olaya, Riyadh",
    "House {n}, Kilimani, Nairobi",
    "{n}-chome, Shibuya, Tokyo",
    "Blk {n} Jurong West St 41, Singapore",
]


def build_corpus(n: int = N) -> list[dict]:
    """Lista de {text, region, expect_gate} con ~70% gate cerrado, ~30% abierto."""
    out: list[dict] = []
    i = 0
    while len(out) < n:
        if i % 10 < 7:  # 70%
            tpl = _LATAM[i % len(_LATAM)] if i % 3 else _US_UK[i % len(_US_UK)]
            region = "LATAM_US"
            expect_gate = False
            if tpl in _US_UK or "{n} 1st" in tpl or "Oxford" in tpl or "King Street" in tpl:
                region = "US_UK"
            text = tpl.format(n=100 + (i % 9000))
        else:
            tpl = _GATE_OPEN[i % len(_GATE_OPEN)]
            region = "HARD"
            expect_gate = True
            text = tpl.format(n=1 + (i % 90))
        out.append({"text": text, "region": region, "expect_gate": expect_gate})
        i += 1
    return out[:n]


_WORKER = textwrap.dedent(r'''
import json, sys, time, tracemalloc

def rss_mb():
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        import resource
        # macOS: bytes; Linux: KB
        val = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return val / (1024 * 1024)
        return val / 1024

def main():
    payload = json.load(sys.stdin)
    mode = payload["mode"]
    addresses = payload["addresses"]

    from smart_import.addresses import (
        EnhancingAddressParser, HeuristicAddressParser, LibpostalAddressParser,
        needs_enhancement,
    )
    from smart_import.addresses.libpostal_parser import is_loaded

    rss0 = rss_mb()
    t0 = time.perf_counter()

    if mode == "off":
        parser = HeuristicAddressParser()
        enhancer_calls = enhancer_skips = fields_added = 0
        roads = 0
        for item in addresses:
            r = parser.parse(item["text"])
            if r.get("road"):
                roads += 1
            if needs_enhancement(r):
                enhancer_skips += 0  # no enhancer
        # "would_gate" count without calling libpostal
        would_gate = sum(
            1 for item in addresses
            if needs_enhancement(HeuristicAddressParser().parse(item["text"]))
        )
        enhancer_skips = len(addresses)  # never called
        stats = {
            "enhancer_calls": 0,
            "enhancer_skips": enhancer_skips,
            "fields_added": 0,
            "would_gate": would_gate,
            "roads": roads,
        }
        loaded_after = is_loaded()
    elif mode == "enhanced":
        heuristic = HeuristicAddressParser()
        libpostal = LibpostalAddressParser()
        parser = EnhancingAddressParser(heuristic, libpostal)
        roads = 0
        first_call_ms = None
        warm_enhancer_ms = 0.0
        warm_enhancer_n = 0
        for idx, item in enumerate(addresses):
            t_call = time.perf_counter()
            prev_calls = parser.enhancer_calls
            r = parser.parse(item["text"])
            dt = (time.perf_counter() - t_call) * 1000
            if parser.enhancer_calls > prev_calls:
                if first_call_ms is None:
                    first_call_ms = dt
                else:
                    warm_enhancer_ms += dt
                    warm_enhancer_n += 1
            if r.get("road"):
                roads += 1
        stats = parser.stats()
        stats["roads"] = roads
        stats["first_enhancer_call_ms"] = first_call_ms
        stats["warm_enhancer_n"] = warm_enhancer_n
        stats["warm_enhancer_per_ms"] = (
            round(warm_enhancer_ms / warm_enhancer_n, 3) if warm_enhancer_n else None
        )
        # Wall sin el cold load (mejor proxy de costo steady-state)
        stats["wall_ms_ex_cold"] = round(
            max(0.0, (time.perf_counter() - t0) * 1000 - (first_call_ms or 0)), 1
        )
        stats["would_gate"] = stats["enhancer_calls"]
        loaded_after = is_loaded()
    elif mode == "enhanced_clean_only":
        # Solo direcciones que NO abren el gate → libpostal NO debe cargarse
        heuristic = HeuristicAddressParser()
        libpostal = LibpostalAddressParser()
        parser = EnhancingAddressParser(heuristic, libpostal)
        roads = 0
        for item in addresses:
            r = parser.parse(item["text"])
            if r.get("road"):
                roads += 1
        stats = parser.stats()
        stats["roads"] = roads
        stats["would_gate"] = 0
        loaded_after = is_loaded()
    elif mode == "libpostal_all":
        # Peor caso: libpostal en TODA direccion (sin gate)
        parser = LibpostalAddressParser()
        assert parser.available()
        roads = 0
        t_first = time.perf_counter()
        parser.parse(addresses[0]["text"])  # cold load
        cold_ms = (time.perf_counter() - t_first) * 1000
        t_warm = time.perf_counter()
        for item in addresses[1:]:
            r = parser.parse(item["text"])
            if r.get("road"):
                roads += 1
        warm_ms = (time.perf_counter() - t_warm) * 1000
        stats = {
            "enhancer_calls": len(addresses),
            "enhancer_skips": 0,
            "fields_added": 0,
            "roads": roads + (1 if parser.parse(addresses[0]["text"]).get("road") else 0),
            "cold_first_ms": cold_ms,
            "warm_rest_ms": warm_ms,
            "warm_per_addr_ms": warm_ms / max(1, len(addresses) - 1),
        }
        loaded_after = is_loaded()
    else:
        raise SystemExit(f"unknown mode {mode}")

    wall_ms = (time.perf_counter() - t0) * 1000
    rss1 = rss_mb()
    out = {
        "mode": mode,
        "n": len(addresses),
        "wall_ms": round(wall_ms, 1),
        "per_addr_ms": round(wall_ms / max(1, len(addresses)), 3),
        "rss_before_mb": round(rss0, 1),
        "rss_after_mb": round(rss1, 1),
        "rss_delta_mb": round(rss1 - rss0, 1),
        "libpostal_loaded": loaded_after,
        **stats,
    }
    json.dump(out, sys.stdout)

if __name__ == "__main__":
    main()
''')


def _run_scenario(mode: str, addresses: list[dict]) -> dict:
    """Subprocess limpio → medicion de RSS fiable."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # Evitar que el .env encienda cosas raras en el worker
    env["SMART_IMPORT_LIBPOSTAL_ENABLED"] = "true" if mode != "off" else "false"
    proc = subprocess.run(
        [sys.executable, "-c", _WORKER],
        input=json.dumps({"mode": mode, "addresses": addresses}),
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
        timeout=600,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"worker mode={mode} failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return json.loads(proc.stdout)


def _print_report(results: dict[str, dict], corpus: list[dict]) -> None:
    gate_open = sum(1 for c in corpus if c["expect_gate"])
    print("\n" + "=" * 72)
    print(f"LIBPOSTAL PERF — {len(corpus)} direcciones  "
          f"(~{100 * gate_open / len(corpus):.0f}% expect_gate abierto)")
    print("=" * 72)
    headers = ("scenario", "wall_ms", "ms/addr", "RSSΔ MB", "loaded?", "calls", "skips")
    print(f"{headers[0]:22} {headers[1]:>10} {headers[2]:>9} "
          f"{headers[3]:>9} {headers[4]:>8} {headers[5]:>6} {headers[6]:>6}")
    for key, label in (
        ("off", "A off (heuristic)"),
        ("clean", "B on + gate cerrado"),
        ("mixed", "C on + mixto 70/30"),
        ("all", "D libpostal en todas"),
    ):
        r = results[key]
        print(
            f"{label:22} {r['wall_ms']:10.1f} {r['per_addr_ms']:9.3f} "
            f"{r['rss_delta_mb']:9.1f} {str(r['libpostal_loaded']):>8} "
            f"{r.get('enhancer_calls', 0):6} {r.get('enhancer_skips', 0):6}"
        )
    print("-" * 72)
    r_off, r_clean, r_mixed, r_all = (
        results["off"], results["clean"], results["mixed"], results["all"],
    )
    print("LECTURA PARA DECIDIR:")
    print(
        f"  • Flag OFF: RSSΔ={r_off['rss_delta_mb']:.0f} MB, "
        f"loaded={r_off['libpostal_loaded']}  → baseline sin postal."
    )
    print(
        f"  • Flag ON pero gate SIEMPRE cerrado: RSSΔ={r_clean['rss_delta_mb']:.0f} MB, "
        f"loaded={r_clean['libpostal_loaded']}  → "
        + ("NO carga datos (solo el wrapper)."
           if not r_clean["libpostal_loaded"]
           else "CARGO datos aunque no debia — bug.")
    )
    print(
        f"  • Mixto (prod-like): calls={r_mixed.get('enhancer_calls')} "
        f"({100 * r_mixed.get('enhancer_calls', 0) / max(1, r_mixed['n']):.0f}%), "
        f"RSSΔ={r_mixed['rss_delta_mb']:.0f} MB (se queda en el proceso)."
    )
    if r_mixed.get("first_enhancer_call_ms") is not None:
        ex = r_mixed.get("wall_ms_ex_cold")
        print(
            f"  • Cold load (1er call real): "
            f"{r_mixed['first_enhancer_call_ms']:.0f} ms una vez por proceso."
        )
        if ex is not None:
            print(
                f"  • Steady-state (500 sin cold): ~{ex:.0f} ms total "
                f"vs off {r_off['wall_ms']:.0f} ms "
                f"(+{ex - r_off['wall_ms']:.0f} ms)."
            )
        warm = r_mixed.get("warm_enhancer_per_ms")
        if warm is not None:
            print(
                f"  • Cada call enhancer YA caliente: ~{warm:.2f} ms "
                f"(n={r_mixed.get('warm_enhancer_n')})."
            )
    if r_all.get("cold_first_ms") is not None:
        print(
            f"  • Peor caso cold 1ra parse: {r_all['cold_first_ms']:.0f} ms; "
            f"warm {r_all.get('warm_per_addr_ms', 0):.3f} ms/addr"
        )
    print(
        "  • Regla: con flag ON, RAM grande (~GB) aparece al PRIMER call real; "
        "si el gate no dispara en todo el proceso, RSS ≈ heuristic."
    )
    print("=" * 72 + "\n")


def test_libpostal_perf_500_decision_report():
    corpus = build_corpus(N)
    assert len(corpus) == N
    clean = [c for c in corpus if not c["expect_gate"]]
    # Por si algun template "clean" igual abre gate: filtrar de verdad
    from smart_import.addresses import HeuristicAddressParser, needs_enhancement
    h = HeuristicAddressParser()
    clean_verified = [
        c for c in clean
        if not needs_enhancement(h.parse(c["text"]))
    ]
    # Si sobran pocos, rellenar con LatAm tipico verificado
    filler_i = 0
    while len(clean_verified) < min(400, N):
        text = f"Av. Corrientes {200 + filler_i}, CABA"
        item = {"text": text, "region": "AR", "expect_gate": False}
        if not needs_enhancement(h.parse(text)):
            clean_verified.append(item)
        filler_i += 1

    results = {
        "off": _run_scenario("off", corpus),
        "clean": _run_scenario("enhanced_clean_only", clean_verified[:400]),
        "mixed": _run_scenario("enhanced", corpus),
        "all": _run_scenario("libpostal_all", corpus),
    }
    _print_report(results, corpus)

    # Invariantes de decision (no flaky por timing)
    assert results["off"]["libpostal_loaded"] is False
    assert results["off"]["rss_delta_mb"] < 200, (
        "flag off no deberia hinchar RSS; algo cargo postal por error"
    )
    assert results["clean"]["libpostal_loaded"] is False, (
        "gate cerrado + flag on NO debe importar postal"
    )
    assert results["clean"]["enhancer_calls"] == 0
    assert results["mixed"]["enhancer_calls"] > 0
    assert results["mixed"]["libpostal_loaded"] is True
    # Una vez cargado, el RSS suele saltar cientos de MB / GB
    assert results["mixed"]["rss_delta_mb"] > 100, (
        "esperabamos salto de RAM al cargar modelos libpostal"
    )
