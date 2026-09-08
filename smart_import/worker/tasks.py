"""De un mensaje de la cola a una llamada al handler.

Es la unica capa que sabe traducir parametros JSON a objetos de dominio, y esta
separada de `handlers.py` a proposito: los handlers son los mismos que corre la
API en modo `embedded`, y no tienen por que enterarse de que existe una cola.

Aca se reconstruye lo que no viaja: el `DepotContext` se arma con la MISMA
funcion que usa el endpoint, y la fecha vuelve de su ISO. Mandar el objeto ya
armado por la cola ataria el formato del mensaje a la forma interna de una
clase, que es lo que se rompe en el primer deploy escalonado.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from ..queue.envelope import GEOCODE, NORMALIZE, InvalidTask, Task
from ..schemas import resolve_schema_path
from .handlers import WorkerContext, run_geocode_job, run_normalize_job


class JobDesaparecido(Exception):
    """El job ya no existe: lo borro el usuario o lo vencio el TTL.

    No es una falla: reintentar no lo va a resucitar. La tarea se confirma y se
    olvida, que es exactamente lo que quiso quien apreto borrar.
    """


def run_task(ctx: WorkerContext, task: Task) -> None:
    if task.type == NORMALIZE:
        return _normalizar(ctx, task)
    if task.type == GEOCODE:
        return _geocodificar(ctx, task)
    raise InvalidTask(f"nadie sabe hacer '{task.type}'")


def _normalizar(ctx: WorkerContext, task: Task) -> None:
    job = ctx.store.get(task.job_id)
    if job is None:
        raise JobDesaparecido(task.job_id)
    p = task.params
    run_normalize_job(
        ctx, job,
        resolve_schema_path(ctx.cfg.schema_dir, job.schema),
        p.get("phone_region"), bool(p.get("diagnostics")),
        manual_mapping=p.get("manual_mapping"),
        timezone=p.get("timezone"),
        depot_timezone=p.get("depot_timezone"),
        service_date=_fecha(p.get("service_date")),
        depot_city=p.get("depot_city"),
        depot_region=p.get("depot_region"),
        depot_country=p.get("depot_country"),
    )


def _geocodificar(ctx: WorkerContext, task: Task) -> None:
    from ..geocoding.depot_context import depot_from_params

    if ctx.store.get(task.job_id) is None:
        raise JobDesaparecido(task.job_id)
    p = task.params
    lat, lon = p.get("origin_lat"), p.get("origin_lon")
    origin = (lat, lon) if lat is not None and lon is not None else None
    bbox = tuple(p["bbox"]) if p.get("bbox") else None
    depot = depot_from_params(
        origin_lat=lat, origin_lon=lon,
        depot_city=p.get("depot_city"), depot_region=p.get("depot_region"),
        depot_postcode=p.get("depot_postcode"), depot_country=p.get("depot_country"),
        depot_address=p.get("depot_address"),
        max_distance_km=float(p.get("max_distance_km")
                              or ctx.cfg.max_geocode_distance_km),
    )
    run_geocode_job(ctx, task.job_id, origin, bbox, p.get("index"), depot=depot,
                    enhance_addresses=bool(p.get("enhance_addresses")))


def _fecha(valor: Any) -> date | None:
    if not valor:
        return None
    if isinstance(valor, date):
        return valor
    try:
        return date.fromisoformat(str(valor))
    except ValueError as exc:
        raise InvalidTask(f"fecha invalida en la tarea: {valor!r}") from exc
