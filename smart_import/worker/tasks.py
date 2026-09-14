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

from ..jobs import HAS_NORMALIZE
from ..queue.envelope import GEOCODE, NORMALIZE, InvalidTask, Task
from ..schemas import resolve_schema_path
from .handlers import WorkerContext, run_geocode_job, run_normalize_job


class JobDesaparecido(Exception):
    """El job ya no existe: lo borro el usuario o lo vencio el TTL.

    No es una falla: reintentar no lo va a resucitar. La tarea se confirma y se
    olvida, que es exactamente lo que quiso quien apreto borrar.
    """


def run_task(ctx: WorkerContext, task: Task) -> None:
    job = ctx.store.get(task.job_id)
    if job is not None and job.operation_task_id and job.operation_task_id != task.task_id:
        # An older delivery must not overwrite a subsequent user operation.
        ctx.logger.info("discarding superseded operation for %s", task.job_id)
        return
    if task.type == NORMALIZE:
        return _normalizar(ctx, task)
    if task.type == GEOCODE:
        return _geocodificar(ctx, task)
    raise InvalidTask(f"nadie sabe hacer '{task.type}'")


def _normalizar(ctx: WorkerContext, task: Task) -> None:
    job = ctx.store.get(task.job_id)
    if job is None:
        raise JobDesaparecido(task.job_id)
    if _ya_esta_hecho(job.normalize_task_id, task, job.status in HAS_NORMALIZE):
        ctx.logger.info("%s ya estaba hecho: se perdio el acuse, no el trabajo",
                        task)
        return
    # Se marca ANTES de trabajar y la guarda exige ademas un estado terminal:
    # asi una tarea que muere a la mitad (queda en `analyzing` o `failed`) se
    # vuelve a hacer, y solo se saltea la que llego hasta el final.
    job.normalize_task_id = task.task_id
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

    job = ctx.store.get(task.job_id)
    if job is None:
        raise JobDesaparecido(task.job_id)
    if _ya_esta_hecho(job.geocode_task_id, task, job.has_current_geocode and job.status == "completed"):
        ctx.logger.info("%s ya estaba hecho: se perdio el acuse, no el trabajo",
                        task)
        return
    job.geocode_task_id = task.task_id
    job.touch("geocoding")
    ctx.store.save(job)
    p = task.params
    from ..geocoding.validation import validate_geo
    try:
        lat, lon = p.get("origin_lat"), p.get("origin_lon")
        if (lat is None) != (lon is None):
            raise ValueError("incomplete origin")
        origin = (lat, lon) if lat is not None else None
        bbox = tuple(p["bbox"]) if p.get("bbox") else None
        distance = p.get("max_distance_km")
        distance = ctx.cfg.max_geocode_distance_km if distance is None else float(distance)
        validate_geo(origin, bbox, distance)
        depot = depot_from_params(
            origin_lat=lat, origin_lon=lon,
            depot_city=p.get("depot_city"), depot_region=p.get("depot_region"),
            depot_postcode=p.get("depot_postcode"), depot_country=p.get("depot_country"),
            depot_address=p.get("depot_address"), max_distance_km=distance,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidTask("Parámetros geográficos inválidos") from exc
    run_geocode_job(ctx, task.job_id, origin, bbox, p.get("index"), depot=depot,
                    enhance_addresses=bool(p.get("enhance_addresses")))


def _ya_esta_hecho(task_id_guardado: str | None, task: Task,
                   hay_resultado: bool) -> bool:
    """Si ESTA tarea ya produjo el resultado que el job tiene ahora.

    Las dos condiciones son necesarias. Solo el id no alcanza: se marca antes
    de trabajar, asi que una tarea que murio a la mitad lo tiene igual y hay que
    rehacerla. Solo el resultado tampoco: un reintento legitimo (que trae otro
    `task_id`) tiene que poder rehacer el trabajo sobre un job que ya tenia
    salida, que es justo lo que hace `PUT /mapping`.
    """
    return bool(task_id_guardado) and task_id_guardado == task.task_id and hay_resultado


def _fecha(valor: Any) -> date | None:
    if not valor:
        return None
    if isinstance(valor, date):
        return valor
    try:
        return date.fromisoformat(str(valor))
    except ValueError as exc:
        raise InvalidTask(f"fecha invalida en la tarea: {valor!r}") from exc
