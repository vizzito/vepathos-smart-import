"""Benchmark reproducible: tiempo por etapa y pico de memoria, por tamano de archivo.

Lo que importa medir por separado:
  - carga del modelo (se paga UNA vez por proceso, no por archivo)
  - inferencia (eso si se paga por archivo)
  - el resto del pipeline, que escala con las filas
"""
from __future__ import annotations

import csv
import gc
import json
import statistics
import time
from pathlib import Path

from .config import Config
from .pipeline import run_normalize


def _peak_rss_mb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        return 0.0


def run_one(path: Path, schema: Path, repeats: int, config: Config) -> dict:
    times: list[float] = []
    stages: list[dict] = []
    rows = 0
    peak = 0.0

    for _ in range(repeats):
        gc.collect()
        before = _peak_rss_mb()
        t0 = time.perf_counter()
        result = run_normalize(path, schema, output_path=None, config=config)
        elapsed = time.perf_counter() - t0
        times.append(elapsed)
        stages.append(result.report["processing_times"])
        rows = result.report["rows_input"]
        peak = max(peak, _peak_rss_mb() - before)

    def stage_avg(name: str) -> float:
        vals = [s.get(name, 0.0) for s in stages]
        return round(statistics.mean(vals), 4) if vals else 0.0

    return {
        "file": path.name,
        "rows": rows,
        "repeats": repeats,
        "ai_enabled": config.ai_enabled,
        "read_s": stage_avg("read"),
        "detect_s": stage_avg("detect"),
        "normalize_s": stage_avg("normalize"),
        "assemble_s": stage_avg("assemble"),
        "total_s": round(statistics.mean(times), 4),
        "median_s": round(statistics.median(times), 4),
        "p95_s": round(sorted(times)[max(0, int(len(times) * 0.95) - 1)], 4),
        "rows_per_s": int(rows / statistics.mean(times)) if times and statistics.mean(times) else 0,
        "delta_rss_mb": round(peak, 1),
    }


def run(files: list[Path], schema: Path, repeats: int = 3, out_dir: str | Path = "benchmarks",
        config: Config | None = None) -> list[dict]:
    cfg = config or Config.from_env()
    results = [run_one(f, schema, repeats, cfg) for f in files if f.exists()]

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    if results:
        with open(out / "results.csv", "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(results[0]), lineterminator="\n")
            w.writeheader()
            w.writerows(results)
    return results


def format_table(results: list[dict]) -> str:
    head = f"{'archivo':26} {'filas':>7} {'read':>7} {'detect':>7} {'norm':>7} {'total':>8} {'filas/s':>9} {'RSS MB':>7}"
    lines = [head, "-" * len(head)]
    for r in results:
        lines.append(
            f"{r['file'][:26]:26} {r['rows']:>7} {r['read_s']:>7.3f} {r['detect_s']:>7.3f} "
            f"{r['normalize_s']:>7.3f} {r['total_s']:>8.3f} {r['rows_per_s']:>9} {r['delta_rss_mb']:>7.1f}"
        )
    return "\n".join(lines)
