"""Mide la CALIDAD real del geocoder, no su velocidad.

Toma un archivo que ya tiene coordenadas buenas, las esconde, geocodifica por
direccion y compara contra la verdad. Responde la pregunta que importa antes de
poner esto en produccion: que cobertura da OSM en las zonas donde opera el cliente.
"""
from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..normalization.values import to_float
from .base import STATUS_LOW, STATUS_MATCHED
from .osm_geocoder import LocalOSMGeocoder
from .scoring import haversine_km


@dataclass
class Evaluation:
    total: int = 0
    matched: int = 0
    low_confidence: int = 0
    not_found: int = 0
    errors_m: list[float] = field(default_factory=list)
    worst: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        located = self.matched + self.low_confidence
        errors = sorted(self.errors_m)
        return {
            "total": self.total,
            "matched": self.matched,
            "low_confidence": self.low_confidence,
            "not_found": self.not_found,
            "coverage_pct": round(100 * located / self.total, 1) if self.total else 0.0,
            "exact_pct": round(100 * self.matched / self.total, 1) if self.total else 0.0,
            "error_m": {
                "median": round(statistics.median(errors), 1) if errors else None,
                "mean": round(statistics.mean(errors), 1) if errors else None,
                "p90": round(errors[int(len(errors) * 0.9)], 1) if len(errors) > 1 else None,
                "max": round(errors[-1], 1) if errors else None,
                "under_100m_pct": (round(100 * sum(1 for e in errors if e <= 100) / len(errors), 1)
                                   if errors else None),
                "under_500m_pct": (round(100 * sum(1 for e in errors if e <= 500) / len(errors), 1)
                                   if errors else None),
            },
            "worst": sorted(self.worst, key=lambda w: -w["error_m"])[:10],
        }


def run(truth_csv: str | Path, index_path: str | Path,
        origin: tuple[float, float] | None = None,
        config: Config | None = None, limit: int | None = None) -> Evaluation:
    cfg = config or Config.from_env()
    with open(truth_csv, "r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))

    geocoder = LocalOSMGeocoder(index_path, match_threshold=cfg.match_threshold,
                                low_threshold=cfg.low_confidence_threshold,
                                aliases_path=cfg.street_aliases_path)
    ev = Evaluation()
    try:
        for row in rows[:limit] if limit else rows:
            address = (row.get("address") or "").strip()
            truth_lat, truth_lon = to_float(row.get("lat")), to_float(row.get("lng"))
            if not address or truth_lat is None or truth_lon is None:
                continue
            ev.total += 1

            result = geocoder.geocode(address, origin=origin)
            if result.status == STATUS_MATCHED:
                ev.matched += 1
            elif result.status == STATUS_LOW:
                ev.low_confidence += 1
            else:
                ev.not_found += 1
                continue

            error_m = haversine_km(truth_lat, truth_lon, result.lat, result.lon) * 1000
            ev.errors_m.append(error_m)
            ev.worst.append({
                "address": address, "error_m": round(error_m, 1),
                "status": result.status, "confidence": round(result.confidence, 3),
                "precision": result.precision,
            })
    finally:
        geocoder.close()
    return ev
