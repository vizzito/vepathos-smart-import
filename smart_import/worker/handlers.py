"""Los handlers del trabajo pesado: normalizar y geolocalizar.

Salieron de `api/app.py` sin cambiarles la logica. Lo unico que cambio es de
donde sacan sus dependencias: antes leian los globals del modulo de la API
(`store`, `CFG`, `logger`), ahora reciben un `WorkerContext`. Sin eso no se
pueden ejecutar fuera del proceso que sirve HTTP.

`run_normalize` y `geocoding.runner.run` no se tocan: ya eran funciones puras de
ruta-entra/ruta-sale con un callback de progreso.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..artifacts import (
    FLAT, GEOCODED, GEOCODED_NESTED, NESTED, RAW, REPORT, ArtifactStore,
)
from ..config import Config
from ..jobs import (
    ANALYZING, COMPLETED, FAILED, GEOCODE_FAILED, GEOCODING, NEEDS_REVIEW,
    NORMALIZED, Job, JobStore,
)
from ..logging_setup import stage
from ..schemas import resolve_schema_path


class NormalizeFailed(Exception):
    """El archivo no se pudo procesar. Error de dominio, no de transporte.

    La API lo traduce a 422 y el worker lo va a tratar como falla permanente (no
    tiene sentido reintentar un .xlsx corrupto diez veces). Antes esto era una
    `HTTPException` levantada desde el pipeline, que ataba el trabajo pesado a
    fastapi.
    """


@dataclass
class WorkerContext:
    """Todo lo que un handler necesita del mundo exterior, explicito.

    Antes eran globals del modulo de la API. Pasarlos por parametro es lo que
    hace que estos handlers se puedan instanciar dos veces en el mismo proceso
    (los tests) o correr en una maquina que no sirve HTTP.
    """

    cfg: Config
    store: JobStore
    artifacts: ArtifactStore
    logger: logging.Logger


def _publish(ctx: WorkerContext, job_id: str, kind: str, path: str | None) -> str | None:
    """Publica una salida del pipeline, salteando las que no se emitieron."""
    if not path:
        return None
    return ctx.artifacts.publish(job_id, kind, path)


def run_normalize_job(ctx: WorkerContext, job: Job, schema_path: Path,
                      phone_region: str | None, diagnostics: bool, manual_mapping: dict | None = None,
                      timezone: str | None = None,
                      depot_timezone: str | None = None,
                      service_date: date | None = None,
                      depot_city: str | None = None,
                      depot_region: str | None = None,
                      depot_country: str | None = None) -> dict[str, Any]:
    from ..extraction.tz import resolve_timezone
    from ..pipeline import run_normalize

    # El raw puede no estar en el disco de este proceso: lo trae quien sepa.
    raw = ctx.artifacts.resolve(job.id, RAW, job.raw_path)
    if raw is None:
        raise NormalizeFailed("el archivo original ya no esta disponible")
    output = ctx.artifacts.reserve(job.id, FLAT)
    resolved_tz = resolve_timezone(timezone, depot_timezone, job.timezone)

    # OCUPADO mientras se trabaja, no `normalized`. Cuando el normalize corria
    # inline nadie podia ver este estado intermedio; con el trabajo en otro nodo,
    # `busy` es lo UNICO que mira la API para saber si ya hay resultado, y
    # anunciar el final al empezar le hace devolver un job con el report vacio.
    job.touch(ANALYZING)
    ctx.store.save(job)
    try:
        result = run_normalize(
            raw, schema_path, output, emit=("flat", "nested"),
            manual_mapping=manual_mapping, phone_region=phone_region,
            diagnostics=diagnostics, timezone=resolved_tz,
            service_date=service_date,
            depot_city=depot_city, depot_region=depot_region,
            depot_country=depot_country,
        )
    except Exception as exc:
        job.error = str(exc)
        job.touch(FAILED)
        ctx.store.save(job)
        ctx.logger.exception("normalize fallo en %s", job.id)
        raise NormalizeFailed(str(exc)) from exc

    job.report = result.report
    job.normalized_path = _publish(ctx, job.id, FLAT, result.outputs.get("flat"))
    job.nested_path = _publish(ctx, job.id, NESTED, result.outputs.get("nested"))
    # El report en disco no lo lee ningun endpoint (el `Job` ya lo tiene en
    # memoria), pero se publica igual: es el unico registro del normalize que
    # sobrevive al proceso.
    _publish(ctx, job.id, REPORT, result.outputs.get("report"))
    job.phone_region = phone_region
    job.timezone = resolved_tz
    job.touch(NEEDS_REVIEW if result.report.get("needs_review") else NORMALIZED)
    ctx.store.save(job)
    return job.as_dict()


def run_geocode_job(ctx: WorkerContext, job_id: str, origin, box,
                    index_name: str | None, depot=None,
                    enhance_addresses: bool = False) -> None:
    from ..geocoding.extract import ExtractError, ensure_geocode_index_from_config
    from ..geocoding.locality import fill_depot_from_index
    from ..geocoding.runner import run

    job = ctx.store.get(job_id)
    if job is None:
        return
    cfg = ctx.cfg
    job.op_started_at = time.time()
    job.touch(GEOCODING)
    ctx.store.save(job)

    try:
        from ..geocoding.depot_context import align_depot_to_geolocator
        depot = align_depot_to_geolocator(depot)
        if depot is not None and depot.origin is not None:
            origin = depot.origin

        country_slug = None
        if index_name:
            index_path = Path(cfg.index_dir) / index_name
            if not index_path.exists():
                raise FileNotFoundError(f"no existe el indice {index_path}")
        else:
            lat = origin[0] if origin else None
            lon = origin[1] if origin else None
            zone_hint = None
            if depot is not None:
                # Preferí ciudad/región; country ISO-2 ("AR") solo como fallback.
                # El registry expande ISO-2 → nombre y NUNCA hace substring corto
                # ("ar" ∈ "ashmore-cartier" era el bug de Tandil).
                zone_hint = (depot.city or depot.region or depot.country or None)
                if zone_hint:
                    zone_hint = str(zone_hint).strip() or None

            def _index_progress(phase: str, pbf: str = "", **_kw) -> None:
                job.geocode_progress = {
                    "phase": phase, "pbf": pbf,
                    "done": 0, "total": int(job.report.get("rows_output") or 0),
                }
                job.touch()
                ctx.store.save_progress(job)

            try:
                ready = ensure_geocode_index_from_config(
                    cfg, lat=lat, lon=lon, bbox=box, zone_hint=zone_hint,
                    progress=_index_progress,
                )
            except ExtractError as exc:
                raise FileNotFoundError(str(exc)) from exc
            index_path = ready.path
            country_slug = ready.country_slug
            stage(ctx.logger, "GEOCODE", "PBF elegido",
                  pbf=ready.entry.path.name, pais=ready.country_slug,
                  extract=ready.entry.key if ready.entry.has_bbox else None,
                  zone_hint=zone_hint,
                  cut=ready.cut_extract)

        # Ciudad/CP del depot desde el indice (Tandil, B7000, …) si el cliente
        # solo mando lat/lon. Sin esto enrich=[] y "Dufau 1418" no geocodifica.
        depot = fill_depot_from_index(depot, index_path, country_slug=country_slug)
        if depot and depot.enrichment_tokens():
            stage(ctx.logger, "GEOCODE", "contexto depot",
                  enrich=depot.enrichment_tokens())

        total = int(job.report.get("rows_output") or 0)

        def progress(done: int, report) -> None:
            job.geocode_progress = {
                "phase": "geocoding", "done": done, "total": total,
                "matched": report.matched, "low_confidence": report.low_confidence,
                "not_found": report.not_found,
                "rejected_far": getattr(report, "rejected_far", 0),
            }
            job.touch()
            # Una escritura POR FILA. Contra un store remoto se limita sola:
            # ver `save_progress`.
            ctx.store.save_progress(job)

        normalizado = ctx.artifacts.resolve(job_id, FLAT, job.normalized_path)
        if normalizado is None:
            raise FileNotFoundError("el archivo normalizado ya no esta disponible")
        output = ctx.artifacts.reserve(job_id, GEOCODED)

        report = run(normalizado, output, index_path, origin=origin, bbox=box,
                     config=cfg, progress=progress, depot=depot,
                     enhance_addresses=bool(enhance_addresses))
        job.geocoded_path = ctx.artifacts.publish(job_id, GEOCODED, output)
        job.geocode_report = report.as_dict()

        # Actualizar needs_geocode residual: las not_found siguen pendientes
        # de ubicacion manual (o de un reintento). El normalize NO se pierde.
        remaining = int(report.not_found or 0) + int(report.errors or 0)
        if isinstance(job.report, dict):
            job.report = {**job.report, "needs_geocode": remaining}

        # El nested se genero durante normalize, ANTES de tener coordenadas. Si no
        # se regenera, `download?format=nested` (que es lo que consume la UI)
        # devuelve la version vieja y todo el geocoding queda invisible.
        refresh_nested(ctx, job, output)
        job.geocode_progress = {"phase": "done", "done": report.rows, "total": report.rows}
        job.op_started_at = None
        # Si quedaron sin coords, COMPLETED igual — la UI las pide a mano.
        job.touch(COMPLETED)
        ctx.store.save(job)
    except Exception as exc:
        # Normalize NO se pierde: el job vuelve a un estado descargable y la UI
        # puede pedir geolocalizacion manual para las filas sin coords.
        job.error = str(exc)
        job.geocode_progress = {"phase": "failed",
                                "done": (job.geocode_progress or {}).get("done", 0),
                                "total": (job.geocode_progress or {}).get("total", 0)}
        job.op_started_at = None
        # GEOCODE_FAILED (no FAILED): el normalize sigue descargable / usable
        job.touch(GEOCODE_FAILED)
        ctx.store.save(job)
        ctx.logger.exception("geocode fallo en %s (normalize conservado)", job_id)


def refresh_nested(ctx: WorkerContext, job: Job, geocoded_csv: Path) -> None:
    """Regenera el JSON anidado a partir del CSV geocodificado.

    El CSV geocodificado ya esta en formato Vepathos, asi que vuelve a pasar por
    el pipeline sin cambios (round-trip) y sale el nested con las coordenadas.
    """
    from ..pipeline import run_normalize

    try:
        # El nested sale del stem de esta ruta (`geocoded.csv` →
        # `geocoded.nested.json`), asi que se reserva el CSV aunque no se
        # reescriba: es la forma de decirle al pipeline donde emitir.
        destino = ctx.artifacts.reserve(job.id, GEOCODED)
        resultado = run_normalize(
            geocoded_csv, resolve_schema_path(ctx.cfg.schema_dir, job.schema), destino,
            emit=("nested",), config=ctx.cfg,
            # El CSV ya es Vepathos flat con address enriquecida: re-detectar
            # "varios campos" y extraer destruye filas (32→11). Solo round-trip.
            expand_composite=False,
        )
        if nested := resultado.outputs.get("nested"):
            job.nested_path = ctx.artifacts.publish(job.id, GEOCODED_NESTED, nested)
            ctx.store.save(job)
            stage(ctx.logger, "EMIT", "nested regenerado con las coordenadas nuevas",
                  entregas=len(resultado.deliveries))
    except Exception as exc:
        # que falle el refresco no puede invalidar un geocoding que salio bien
        stage(ctx.logger, "WARN", f"no se pudo regenerar el nested tras geocodificar: {exc}",
              level=logging.WARNING)
