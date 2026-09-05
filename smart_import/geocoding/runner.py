"""Accion `geocode`: toma un archivo YA normalizado y completa las coordenadas.

Es deliberadamente una operacion aparte de `normalize`:
  - solo se tocan las filas que tienen direccion y NO tienen coordenadas validas
  - las filas que ya venian con coordenadas se conservan intactas
  - un match debil NUNCA se escribe como si fuera bueno
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..logging_setup import detail, get_logger, stage
from ..normalization.values import render, to_float
from .base import (
    STATUS_ALREADY, STATUS_ERROR, STATUS_LOW, STATUS_MATCHED, STATUS_NOT_FOUND,
    GeocodeResult,
)
from .cache import GeocodeCache
from .osm_geocoder import LocalOSMGeocoder

logger = get_logger("geocode")

DIAGNOSTIC_COLUMNS = ("geocode_status", "geocode_confidence", "geocode_precision",
                      "geocode_source")


@dataclass
class GeocodeReport:
    rows: int = 0
    already_geocoded: int = 0
    matched: int = 0
    low_confidence: int = 0
    not_found: int = 0
    errors: int = 0
    cache: dict = field(default_factory=dict)
    elapsed_s: float = 0.0
    index: str = ""
    origin: dict | None = None
    bbox: dict | None = None
    samples: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rows": self.rows,
            "already_geocoded": self.already_geocoded,
            "matched": self.matched,
            "low_confidence": self.low_confidence,
            "not_found": self.not_found,
            "errors": self.errors,
            "needs_review": self.low_confidence + self.not_found,
            "cache": self.cache,
            "elapsed_s": round(self.elapsed_s, 3),
            "rows_per_s": int(self.rows / self.elapsed_s) if self.elapsed_s else 0,
            "index": self.index,
            "origin": self.origin, "bbox": self.bbox,
            "samples": self.samples[:50],
        }


def _valid_coords(lat, lon) -> bool:
    lat_f, lon_f = to_float(lat), to_float(lon)
    return (lat_f is not None and lon_f is not None
            and -90 <= lat_f <= 90 and -180 <= lon_f <= 180)


def run(input_path: str | Path, output_path: str | Path, index_path: str | Path,
        origin: tuple[float, float] | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        config: Config | None = None,
        cache_path: str | Path | None = None,
        progress=None) -> GeocodeReport:
    cfg = config or Config.from_env()
    started = time.perf_counter()

    src, dst = Path(input_path), Path(output_path)
    with open(src, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"{src} no tiene filas")

    columns = list(rows[0].keys())
    for col in ("lat", "lng", *DIAGNOSTIC_COLUMNS):
        if col not in columns:
            columns.append(col)

    stage(logger, "GEOCODE", "abriendo indice", indice=Path(index_path).name,
          filas=len(rows), depot=f"{origin[0]},{origin[1]}" if origin else None,
          bbox="si" if bbox else None)
    geocoder = LocalOSMGeocoder(index_path, match_threshold=cfg.match_threshold,
                                low_threshold=cfg.low_confidence_threshold)
    cache = GeocodeCache(cache_path or cfg.cache_path)
    stage(logger, "GEOCODE", "umbrales", matched=f">={cfg.match_threshold}",
          low_confidence=f">={cfg.low_confidence_threshold}", fallback_externo=cfg.geocoder_fallback)
    context = Path(index_path).stem
    report = GeocodeReport(rows=len(rows), index=str(index_path))
    if origin:
        report.origin = {"lat": origin[0], "lon": origin[1]}
    if bbox:
        report.bbox = dict(zip(("north", "south", "east", "west"), bbox))

    try:
        for i, row in enumerate(rows, start=1):
            address = (row.get("address") or "").strip()

            if _valid_coords(row.get("lat"), row.get("lng")):
                _stamp(row, GeocodeResult(status=STATUS_ALREADY))
                report.already_geocoded += 1
                continue                          # ya tenia coordenadas: no se toca

            if not address:
                _stamp(row, GeocodeResult(status=STATUS_NOT_FOUND,
                                          detail={"reason": "sin direccion"}))
                report.not_found += 1
                continue

            cached = cache.get(address, context)
            result = cached
            if result is None:
                result = geocoder.geocode(address, origin=origin, bbox=bbox)
                cache.put(address, result, context)
            if i <= 12:
                origen = "cache" if cached else "indice"
                coords = (f"{result.lat:.5f},{result.lon:.5f}" if result.has_coords else "-")
                detail(logger, f"{address[:38]:<40} {result.status:<14} "
                               f"{result.confidence:.2f} {str(result.precision or ''):<11} "
                               f"{coords:<22} ({origen})")

            if result.status in (STATUS_MATCHED, STATUS_LOW) and result.has_coords:
                row["lat"] = render(result.lat)
                row["lng"] = render(result.lon)
            _stamp(row, result)

            if result.status == STATUS_MATCHED:
                report.matched += 1
            elif result.status == STATUS_LOW:
                report.low_confidence += 1
                _sample(report, row, address, result)
            elif result.status == STATUS_ERROR:
                report.errors += 1
                _sample(report, row, address, result)
            else:
                report.not_found += 1
                _sample(report, row, address, result)

            if progress and (i == 1 or i % 25 == 0 or i == len(rows)):
                progress(i, report)
        cache.commit()
    finally:
        cache_stats = cache.stats()
        cache.close()
        geocoder.close()

    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})

    report.cache = cache_stats
    report.elapsed_s = time.perf_counter() - started
    stage(logger, "GEOCODE", "",
          ya_tenian=report.already_geocoded, geocodificadas=report.matched,
          confianza_baja=report.low_confidence, no_encontradas=report.not_found,
          errores=report.errors or None)
    stage(logger, "GEOCODE", "cache", hits=cache_stats["hits"],
          misses=cache_stats["misses"], hit_rate=f"{cache_stats['hit_rate']:.0%}")
    stage(logger, "DONE", "geocode terminado", salida=str(dst),
          t=f"{report.elapsed_s:.3f}s", filas_por_s=int(report.rows / report.elapsed_s)
          if report.elapsed_s else 0)
    return report


def _stamp(row: dict, result: GeocodeResult) -> None:
    row["geocode_status"] = result.status
    row["geocode_confidence"] = f"{result.confidence:.3f}" if result.confidence else ""
    row["geocode_precision"] = result.precision or ""
    row["geocode_source"] = result.source or ""


def _sample(report: GeocodeReport, row: dict, address: str, result: GeocodeResult) -> None:
    report.samples.append({
        "delivery_id": row.get("delivery_id"), "address": address,
        "status": result.status, "confidence": round(result.confidence, 3),
        "detail": result.detail,
    })
