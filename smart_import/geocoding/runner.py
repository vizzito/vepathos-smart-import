"""Accion `geocode`: toma un archivo YA normalizado y completa las coordenadas.

Es deliberadamente una operacion aparte de `normalize`:
  - solo se tocan las filas que tienen direccion y NO tienen coordenadas validas
  - las filas que ya venian con coordenadas se conservan intactas
  - un match debil NUNCA se escribe como si fuera bueno
  - el depot enriquece la query y aplica geofence (radio maximo)
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
from ..addresses.scoring import AddressCandidateScorer
from .bands import BAND_NEEDS_GEOCODING, BAND_REVIEW, BAND_VALID, band_for
from .cache import GeocodeCache
from .depot_context import DepotContext
from .osm_geocoder import LocalOSMGeocoder
from .query import (
    build_geocode_query, cleaned_query, is_weak_result, pick_better,
)

logger = get_logger("geocode")

#: `geocode_band` es lo que colorea la UI. Se calcula ACA para que no haya dos
#: implementaciones de la misma regla desincronizandose (ver geocoding/bands.py).
DIAGNOSTIC_COLUMNS = (
    "geocode_status", "geocode_confidence", "geocode_band",
    "geocode_precision", "geocode_source", "geocode_raw_score", "geocode_reason",
)


@dataclass
class GeocodeReport:
    rows: int = 0
    already_geocoded: int = 0
    matched: int = 0
    low_confidence: int = 0
    not_found: int = 0
    errors: int = 0
    rejected_far: int = 0
    skipped_low_confidence: int = 0
    enriched: int = 0
    cache: dict = field(default_factory=dict)
    elapsed_s: float = 0.0
    index: str = ""
    origin: dict | None = None
    bbox: dict | None = None
    depot: dict | None = None
    samples: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rows": self.rows,
            "already_geocoded": self.already_geocoded,
            "matched": self.matched,
            "low_confidence": self.low_confidence,
            "not_found": self.not_found,
            "errors": self.errors,
            "rejected_far": self.rejected_far,
            "skipped_low_confidence": self.skipped_low_confidence,
            "enriched": self.enriched,
            "needs_review": self.low_confidence + self.not_found,
            "cache": self.cache,
            "elapsed_s": round(self.elapsed_s, 3),
            "rows_per_s": int(self.rows / self.elapsed_s) if self.elapsed_s else 0,
            "index": self.index,
            "origin": self.origin, "bbox": self.bbox,
            "depot": self.depot,
            "samples": self.samples[:50],
        }


def _valid_coords(lat, lon) -> bool:
    lat_f, lon_f = to_float(lat), to_float(lon)
    return (lat_f is not None and lon_f is not None
            and -90 <= lat_f <= 90 and -180 <= lon_f <= 180)


def _reject_far(result: GeocodeResult, far_km: float, max_km: float,
                reason: str) -> GeocodeResult:
    return GeocodeResult(
        status=STATUS_NOT_FOUND,
        confidence=result.confidence,
        precision=result.precision,
        source=result.source,
        detail={**(result.detail or {}),
                "rejected_far_from_depot_km": round(far_km, 1),
                "max_km": max_km,
                "reject_reason": reason},
    )


def run(input_path: str | Path, output_path: str | Path, index_path: str | Path,
        origin: tuple[float, float] | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        config: Config | None = None,
        cache_path: str | Path | None = None,
        progress=None,
        depot: DepotContext | None = None,
        enhance_addresses: bool = False) -> GeocodeReport:
    cfg = config or Config.from_env()
    started = time.perf_counter()

    # Retrocompat: origin=(lat,lon) sigue siendo valido; depot lo subsume.
    if depot is None and origin is not None:
        depot = DepotContext(
            lat=origin[0], lon=origin[1],
            max_distance_km=float(cfg.max_geocode_distance_km),
        )
    elif depot is not None and depot.max_distance_km <= 0:
        depot = DepotContext(
            lat=depot.lat, lon=depot.lon, city=depot.city, region=depot.region,
            postcode=depot.postcode, country=depot.country, address=depot.address,
            max_distance_km=float(cfg.max_geocode_distance_km),
        )

    effective_origin = depot.origin if depot else origin

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
          filas=len(rows),
          depot=f"{effective_origin[0]},{effective_origin[1]}" if effective_origin else None,
          bbox="si" if bbox else None,
          enrich=",".join(depot.enrichment_tokens()) if depot else None,
          enhance_addresses=bool(enhance_addresses) or None)
    geocoder = LocalOSMGeocoder(
        index_path, match_threshold=cfg.match_threshold,
        low_threshold=cfg.low_confidence_threshold,
        street_level_floor=cfg.geocode_street_level_floor,
        street_match_min=cfg.geocode_street_match_min,
        review_band=cfg.geocode_review_band, valid_band=cfg.geocode_valid_band,
        soft_reject=cfg.geocode_soft_reject,
        soft_reject_min=cfg.geocode_soft_reject_min)
    cache = GeocodeCache(cache_path or cfg.cache_path)
    stage(logger, "GEOCODE", "umbrales", matched=f">={cfg.match_threshold}",
          low_confidence=f">={cfg.low_confidence_threshold}",
          banda_verde=f">={cfg.geocode_valid_band}",
          banda_ambar=f">={cfg.geocode_review_band}",
          piso_nivel_calle=cfg.geocode_street_level_floor,
          calle_minima=cfg.geocode_street_match_min,
          soft_reject=("on" if cfg.geocode_soft_reject else "off")
                       + f">={cfg.geocode_soft_reject_min}",
          geofence_km=depot.max_distance_km if depot else None,
          low_conf_km=cfg.max_low_confidence_km,
          fallback_externo=cfg.geocoder_fallback)
    # Incluir umbrales + enhance en la clave: no reusar cache de otra semantica.
    context = (
        f"{Path(index_path).stem}"
        f"|m{cfg.match_threshold:.3f}"
        f"|l{cfg.low_confidence_threshold:.3f}"
        f"|v{cfg.geocode_valid_band:.3f}"
        f"|r{cfg.geocode_review_band:.3f}"
        f"|s{cfg.geocode_street_level_floor:.3f}"
        f"|c{cfg.geocode_street_match_min:.3f}"
        f"|sr{int(cfg.geocode_soft_reject)}{cfg.geocode_soft_reject_min:.3f}"
        f"|e{int(bool(enhance_addresses))}"
        f"|q2"  # query-only enrich (no muta address)
    )
    report = GeocodeReport(rows=len(rows), index=str(index_path))
    if effective_origin:
        report.origin = {"lat": effective_origin[0], "lon": effective_origin[1]}
    if bbox:
        report.bbox = dict(zip(("north", "south", "east", "west"), bbox))
    if depot:
        report.depot = depot.as_dict()

    scorer = AddressCandidateScorer()
    address_min = float(getattr(cfg, "address_accept_threshold", 0.50))
    bands = (cfg.geocode_valid_band, cfg.geocode_review_band)
    retries = 0

    try:
        for i, row in enumerate(rows, start=1):
            display = (row.get("address") or "").strip()

            # Query interna: address + localidad de fila + depot. NUNCA pisa display.
            query = build_geocode_query(
                display, row=row, depot=depot, enhance=enhance_addresses)
            if query != display:
                report.enriched += 1

            if _valid_coords(row.get("lat"), row.get("lng")):
                _stamp(row, GeocodeResult(status=STATUS_ALREADY), bands, has_coords=True)
                report.already_geocoded += 1
                continue

            if not display:
                _stamp(row, GeocodeResult(status=STATUS_NOT_FOUND,
                                          detail={"reason": "sin direccion"}), bands)
                report.not_found += 1
                continue

            evidence = scorer.score(display)
            if evidence.score < address_min:
                _stamp(row, GeocodeResult(
                    status=STATUS_NOT_FOUND,
                    detail={"reason": "sin evidencia suficiente de direccion",
                            "address_score": round(evidence.score, 3),
                            "evidence": list(evidence.evidence)}), bands)
                report.skipped_low_confidence += 1
                _sample(report, row, display,
                        GeocodeResult(status=STATUS_NOT_FOUND,
                                      detail={"address_score": round(evidence.score, 3)}))
                continue

            result = _lookup(cache, geocoder, query, context, effective_origin, bbox)

            # Reintento limpio (sin piso/depto) si el primero es flojo.
            if is_weak_result(result, review_band=cfg.geocode_review_band):
                alt = cleaned_query(query)
                if alt and alt.casefold() != query.casefold():
                    alt_result = _lookup(
                        cache, geocoder, alt, context, effective_origin, bbox)
                    better = pick_better(result, alt_result)
                    if better is not None and better is not result:
                        retries += 1
                        result = better
                        query = alt

            # Defensa: _lookup / pick_better nunca deberían dejar None acá.
            if result is None:
                result = GeocodeResult(
                    status=STATUS_NOT_FOUND,
                    detail={"reason": "geocode_returned_none"},
                )

            if result.status in (STATUS_MATCHED, STATUS_LOW) and result.has_coords:
                result = _apply_depot_guards(
                    result, depot=depot, cfg=cfg, report=report,
                )
                if result.status == STATUS_NOT_FOUND:
                    _stamp(row, result, bands)
                    report.not_found += 1
                    _sample(report, row, display, result, query=query)
                    if i <= 12:
                        _log_row(display, row, cached=False, query=query)
                    if progress and (i == 1 or i % 25 == 0 or i == len(rows)):
                        progress(i, report)
                    continue
                row["lat"] = render(result.lat)
                row["lng"] = render(result.lon)
            _stamp(row, result, bands)

            band = row.get("geocode_band") or BAND_NEEDS_GEOCODING
            if band == BAND_VALID:
                report.matched += 1
            elif band == BAND_REVIEW:
                report.low_confidence += 1
                _sample(report, row, display, result, query=query)
            elif result.status == STATUS_ERROR:
                report.errors += 1
                _sample(report, row, display, result, query=query)
            else:
                report.not_found += 1
                _sample(report, row, display, result, query=query)

            if i <= 12:
                _log_row(display, row, cached=False, query=query)

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
          ya_tenian=report.already_geocoded or None,
          valid=report.matched, review=report.low_confidence,
          needs_geocoding=report.not_found,
          fuera_de_radio=report.rejected_far or None,
          sin_evidencia=report.skipped_low_confidence or None,
          enriquecidas=report.enriched or None,
          reintentos_limpios=retries or None,
          enhance=bool(enhance_addresses) or None,
          errores=report.errors or None)
    stage(logger, "GEOCODE", "cache", hits=cache_stats["hits"],
          misses=cache_stats["misses"], hit_rate=f"{cache_stats['hit_rate']:.0%}")
    stage(logger, "DONE", "geocode terminado", salida=str(dst),
          t=f"{report.elapsed_s:.3f}s", filas_por_s=int(report.rows / report.elapsed_s)
          if report.elapsed_s else 0)
    return report


def _lookup(cache, geocoder, query: str, context: str, origin, bbox) -> GeocodeResult:
    cached = cache.get(query, context)
    if cached is not None:
        return cached
    result = geocoder.geocode(query, origin=origin, bbox=bbox)
    cache.put(query, result, context)
    return result


def _apply_depot_guards(result: GeocodeResult, *, depot: DepotContext | None,
                        cfg: Config, report: GeocodeReport) -> GeocodeResult:
    """Geofence duro (max_geocode_distance_km) + soft low_confidence cercano."""
    if depot is None or result.lat is None or result.lon is None:
        return result

    far_km = depot.distance_km(result.lat, result.lon)
    if far_km is None:
        return result

    hard_max = float(depot.max_distance_km or cfg.max_geocode_distance_km)
    if far_km > hard_max:
        report.rejected_far += 1
        return _reject_far(result, far_km, hard_max, "outside_operating_radius")

    # low_confidence lejos del depot (pero dentro del hard max) = falso positivo tipico
    soft_max = float(cfg.max_low_confidence_km)
    if result.status == STATUS_LOW and far_km > soft_max:
        report.rejected_far += 1
        return _reject_far(result, far_km, soft_max, "low_confidence_far_from_depot")

    return result


def _log_row(address: str, row: dict, *, cached: bool, query: str | None = None) -> None:
    """Log post-stamp: banda + coords finales (lo mismo que ve la UI)."""
    band = row.get("geocode_band") or "-"
    conf = row.get("geocode_confidence") or row.get("geocode_raw_score") or "0"
    try:
        conf_f = float(conf)
        conf_s = f"{conf_f:.2f}"
    except (TypeError, ValueError):
        conf_s = "0.00"
    lat, lng = row.get("lat") or "", row.get("lng") or ""
    coords = f"{lat},{lng}" if lat and lng else "-"
    prec = row.get("geocode_precision") or ""
    origen = "cache" if cached else "indice"
    label = address[:40]
    if query and query != address:
        label = f"{address[:28]}→{query[:20]}"
    detail(logger, f"{label:<40} {band:<16} {conf_s} {prec:<11} "
                   f"{coords:<22} ({origen})")


def _stamp(row: dict, result: GeocodeResult, bands: tuple[float, float] = (0.80, 0.70),
           has_coords: bool | None = None) -> None:
    """Sella el resultado en la fila, banda incluida.

    `has_coords` se pasa aparte porque una fila que YA venia con coordenadas no
    las trae en el `GeocodeResult`: sin esto quedaria marcada como si hubiera que
    ubicarla a mano.

    `geocode_confidence` = score REAL cuando hay pin.
    Sin pin se publica `geocode_raw_score` / `geocode_reason` para diagnosticar
    candidatos descartados (p.ej. Callao 0.90 sin calle).
    """
    valid_at, review_at = bands
    con_pin = result.has_coords if has_coords is None else has_coords
    raw = result.detail.get("raw_score", result.confidence)
    try:
        raw_f = float(raw) if raw not in (None, "") else 0.0
    except (TypeError, ValueError):
        raw_f = float(result.confidence or 0.0)

    row["geocode_status"] = result.status
    row["geocode_confidence"] = (
        f"{result.confidence:.3f}" if (con_pin and result.confidence) else "")
    soft = bool(result.detail.get("soft_reject"))
    row["geocode_band"] = band_for(
        result.status, result.confidence,
        has_coords=con_pin,
        valid_at=valid_at, review_at=review_at,
        precision=result.precision,
        force_review=soft,  # ignored: banda = score vs umbrales
    )
    # Soft pin debajo del corte ambar: no dejar lat/lng colgados como "listos".
    if row["geocode_band"] == BAND_NEEDS_GEOCODING and con_pin and result.status != STATUS_ALREADY:
        row["lat"] = ""
        row["lng"] = ""
        row["geocode_status"] = STATUS_NOT_FOUND
        row["geocode_confidence"] = ""
        row["geocode_raw_score"] = f"{raw_f:.3f}" if raw_f else ""
        row["geocode_precision"] = result.precision or ""
        row["geocode_source"] = result.source or ""
        reason = result.detail.get("reason") or "below_review_band"
        row["geocode_reason"] = str(reason)[:240]
        return
    row["geocode_precision"] = result.precision or ""
    row["geocode_source"] = result.source or ""
    row["geocode_raw_score"] = f"{raw_f:.3f}" if raw_f else ""
    reason = result.detail.get("reason") or ""
    row["geocode_reason"] = str(reason)[:240]


def _sample(report: GeocodeReport, row: dict, address: str, result: GeocodeResult,
            query: str | None = None) -> None:
    sample = {
        "delivery_id": row.get("delivery_id"), "address": address,
        "status": result.status, "confidence": round(result.confidence, 3),
        "detail": result.detail,
    }
    if query and query != address:
        sample["enriched_query"] = query
    report.samples.append(sample)
