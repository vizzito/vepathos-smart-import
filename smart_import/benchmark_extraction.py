"""Benchmark de extraccion: reglas vs libpostal vs hibrido.

Mide lo que importa para decidir, no lo que es facil de medir:

  * costo   tiempo, filas/s, CPU (user+sys), pico de RSS
  * calidad accuracy por campo contra un gold, falsos positivos, ignorados OK
  * modelo  llamadas de IA (tiene que ser 0 en el camino normal)

"""
from __future__ import annotations

import gc
import json
import resource
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Config
from .extraction.context import ExtractionContext
from .extraction.free_text import FreeTextExtractor

CORPUS = Path("examples/free-text")


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _rss_mb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        return 0.0


@dataclass
class Accuracy:
    """Aciertos por campo contra el gold, mas los errores que mas duelen."""
    expected: int = 0
    extracted: int = 0
    false_positives: int = 0
    ignored_expected: int = 0
    ignored_correct: int = 0
    needs_review: int = 0
    by_field: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        out = {
            "expected": self.expected, "extracted": self.extracted,
            "false_positives": self.false_positives,
            "ignored_expected": self.ignored_expected,
            "ignored_correct": self.ignored_correct,
            "needs_review": self.needs_review,
        }
        for name, hits in sorted(self.by_field.items()):
            out[f"{name}_ok"] = hits
            out[f"{name}_pct"] = round(100.0 * hits / self.expected, 1) if self.expected else 0.0
        return out


def _same_phone(found: str | None, wanted: str) -> bool:
    """Compara por digitos: encontrar el telefono y formatearlo son cosas distintas.

    Sin esto el modelo saldria con 0% solo por devolver '11-4000-1000' en vez del
    E.164, que es trabajo del normalizador, no del extractor.
    """
    import re
    a = re.sub(r"\D", "", found or "")
    b = re.sub(r"\D", "", wanted or "")
    return bool(a) and (a[-10:] == b[-10:])


def score_against_gold(records, ignored, gold: dict) -> Accuracy:
    """Compara con el gold. address se valida por contenido, no por igualdad."""
    expected = gold["records"]
    acc = Accuracy(expected=len(expected), extracted=len(records),
                   ignored_expected=len(gold.get("ignored", [])))
    acc.ignored_correct = sum(
        1 for wanted in gold.get("ignored", [])
        if any(wanted[:18].lower() in item["text"].lower() for item in ignored))
    acc.needs_review = sum(1 for r in records if r.status == "needs_review")

    by_name: dict[str, list] = {}
    for record in records:
        by_name.setdefault((record.get("customer_name") or "").lower(), []).append(record)

    hits = {"customer_name": 0, "address": 0, "phone": 0, "tw_end": 0}
    matched = set()
    for wanted in expected:
        found = next((r for r in by_name.get(wanted["customer_name"].lower(), [])
                      if id(r) not in matched), None)
        if found is None:
            continue
        matched.add(id(found))
        hits["customer_name"] += 1
        if wanted["address_contains"] in (found.get("address") or ""):
            hits["address"] += 1
        if _same_phone(found.get("phone"), wanted["phone"]):
            hits["phone"] += 1
        hour = wanted["tw_end_hour"]
        end = found.get("tw_end") or ""
        if (hour is None and not end) or (hour is not None and end.endswith(f"{hour:02d}:00")):
            hits["tw_end"] += 1

    acc.by_field = hits
    acc.false_positives = len(records) - len(matched)
    return acc


def run_strategy(document: str, gold: dict | None, config: Config,
                 region: str | None, repeats: int, service_date: date | None) -> dict:
    """Una corrida completa con una estrategia de parser de direcciones."""
    context = ExtractionContext(phone_region=region)
    extractor = FreeTextExtractor(config, context, service_date=service_date)

    gc.collect()
    rss_before, cpu_before = _rss_mb(), _cpu_seconds()
    started = time.perf_counter()
    result = None
    for _ in range(repeats):
        result = extractor.run_document(document)
    elapsed = (time.perf_counter() - started) / max(repeats, 1)
    cpu = (_cpu_seconds() - cpu_before) / max(repeats, 1)
    rss = max(_rss_mb() - rss_before, 0.0)

    row = {
        "parser": extractor.fields.address_parser.name,
        "records": len(result.records),
        "ignored": len(result.ignored),
        "elapsed_s": round(elapsed, 4),
        "records_per_s": int(len(result.records) / elapsed) if elapsed else 0,
        "cpu_s": round(cpu, 4),
        "delta_rss_mb": round(rss, 1),
        "ai_calls": result.ai_calls,
    }
    if gold:
        row.update(score_against_gold(result.records, result.ignored, gold).as_dict())
    return row




def run(corpus: Path = CORPUS, parsers: tuple[str, ...] = ("heuristic",),
        repeats: int = 3, region: str | None = "AR",
        out_dir: str | Path = "benchmarks", config: Config | None = None,
        service_date: date | None = None) -> list[dict]:
    cfg = config or Config.from_env()
    documents = sorted(Path(corpus).glob("*.txt"))
    results: list[dict] = []

    for path in documents:
        document = path.read_text(encoding="utf-8")
        gold_path = path.with_suffix("").with_suffix(".expected.json")
        gold = json.loads(gold_path.read_text(encoding="utf-8")) if gold_path.exists() else None

        for strategy in parsers:
            # enhanced/hybrid/libpostal requieren la flag; heuristic nunca.
            wants_libpostal = strategy != "heuristic"
            variant = cfg.replace(address_parser=strategy,
                                  libpostal_enabled=wants_libpostal)
            if wants_libpostal:
                from .addresses import is_installed
                if not is_installed():
                    results.append({"dataset": path.stem, "parser": strategy,
                                    "skipped": "libpostal no esta instalado "
                                               "(ver docs/libpostal.md)"})
                    continue
            row = run_strategy(document, gold, variant, region, repeats, service_date)
            results.append({"dataset": path.stem, **row})


    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "extraction.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return results


def format_table(results: list[dict]) -> str:
    head = (f"{'dataset':22} {'parser':20} {'recs':>5} {'ign':>4} {'ms':>8} "
            f"{'cpu ms':>8} {'RSS MB':>7} {'IA':>4} {'addr%':>6} {'tel%':>6} {'FP':>4}")
    lines = [head, "-" * len(head)]
    for r in results:
        if r.get("skipped"):
            lines.append(f"{r['dataset'][:22]:22} {r['parser'][:20]:20} {r['skipped']}")
            continue
        lines.append(
            f"{r['dataset'][:22]:22} {r['parser'][:20]:20} {r['records']:>5} "
            f"{r['ignored']:>4} {r['elapsed_s'] * 1000:>8.1f} {r['cpu_s'] * 1000:>8.1f} "
            f"{r['delta_rss_mb']:>7.1f} {r['ai_calls']:>4} "
            f"{r.get('address_pct', float('nan')):>6.1f} "
            f"{r.get('phone_pct', float('nan')):>6.1f} "
            f"{r.get('false_positives', 0):>4}")
    return "\n".join(lines)
