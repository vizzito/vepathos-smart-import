"""API HTTP de Smart Import.

Diseno:
  - `normalize` es SINCRONO: 50k filas tardan ~1.5 s, no justifica una cola.
  - `geocode` es ASINCRONO y SIEMPRE explicito: nunca se dispara solo.
  - el archivo subido no viaja mas alla de esta capa; se guarda en disco y el
    resto del pipeline trabaja con rutas.
  - todo el trabajo pesado pasa por un techo de concurrencia (`_normalize_slot`)
    y la puerta de entrada rechaza antes de leer el body si la cola esta llena:
    bajo avalancha el servicio se defiende con 429, no reventando la VM.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import anyio
from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..artifacts import (
    FLAT, GEOCODED, GEOCODED_NESTED, NESTED, RAW, make_artifact_store,
)
from ..config import Config
from ..schemas import (
    SchemaNotFound, TargetSchema, resolve_schema_dir, resolve_schema_path,
)
from ..jobs import ALLOWED_SUFFIXES, Job, safe_filename, make_job_store

from ..logging_setup import get_logger, setup as setup_logging, stage
from ..worker.handlers import (
    NormalizeFailed, WorkerContext, refresh_nested, run_geocode_job,
    run_normalize_job,
)
from .internal import internal_router

CFG = Config.from_env()
setup_logging(verbose=CFG.verbose)
logger = get_logger("api")

# El directorio de schemas es configurable: en el container vive en /app/schemas,
# en desarrollo en ./schemas, y un cliente puede montar los suyos.
SCHEMA_DIR = resolve_schema_dir(CFG.schema_dir)

# Un worker por defecto para cada tarea pesada: este container comparte la VM con
# el cutter y no tiene que competirle CPU. Configurable cuando haya medicion.
_geocode_pool = ThreadPoolExecutor(max_workers=max(1, CFG.geocode_workers),
                                   thread_name_prefix="geocode")

# El servicio se defiende en dos puertas, porque protegen recursos distintos.
#
#   1. ADMISION (`_admission`): cuantos imports pueden estar en el sistema a la
#      vez, contando el que todavia esta subiendo. Se pide ANTES de leer el
#      body, y si esta llena el 429 sale sin tocar disco. Es lo que acota la
#      avalancha: sin esta puerta, 1000 uploads simultaneos escriben hasta
#      1000 x SMART_IMPORT_MAX_FILE_MB en el disco que compartimos con el
#      cutter antes de que el techo de CPU rechace a uno solo.
#
#   2. CPU (`_normalize_slots`): cuantos normalize corren en paralelo. Es un
#      techo, no un acelerador: el normalize es Python CPU-bound y el GIL lo
#      serializa igual (medido: 8 uploads de 5k filas tardan 8x lo que uno,
#      con la CPU en 1 core de 4). Sirve para que sobren hilos del threadpool
#      para /health, el polling y las descargas.
#
# Chequear un contador en vez de tomar un token no alcanzaria para la puerta 1:
# con 1000 requests llegando juntos, los 1000 leen "cola vacia" antes de que el
# primero se encole y pasan todos. El token se toma o no se toma.
_admission = anyio.Semaphore(max(1, CFG.max_normalize_queue))
_normalize_slots = anyio.Semaphore(max(1, CFG.max_concurrent_normalize))
_normalize_in_flight = 0
_normalize_admitted = 0

#: Segundos de Retry-After cuando la admision esta llena. Corto a proposito: el
#: cliente reintenta con backoff, no espera a que se vacie toda la cola.
RETRY_AFTER_FULL_S = 5


@asynccontextmanager
async def _admitted():
    """Reserva un lugar en el sistema, o 429 inmediato sin tocar disco."""
    global _normalize_admitted
    try:
        _admission.acquire_nowait()
    except anyio.WouldBlock:
        stage(logger, "HTTP", "429 admision llena (rechazado antes de leer el archivo)",
              admitidos=_normalize_admitted, cupo=CFG.max_normalize_queue)
        # 429 y no 503 a proposito: 503 le dice al balanceador "esta instancia
        # esta caida" y hay balanceadores que la sacan del pool por eso. 429 es
        # "aflojá", que es exactamente lo que queremos comunicar.
        raise HTTPException(
            429,
            f"el servicio ya tiene {CFG.max_normalize_queue} imports en curso. "
            f"Reintenta en unos segundos.",
            headers={"Retry-After": str(RETRY_AFTER_FULL_S)},
        ) from None
    _normalize_admitted += 1
    try:
        yield
    finally:
        _normalize_admitted -= 1
        _admission.release()


@asynccontextmanager
async def _normalize_slot(job_id: str):
    """Reserva un turno de CPU para normalizar, o 429 si no se libera a tiempo."""
    global _normalize_in_flight
    espera = max(0.0, float(CFG.normalize_queue_wait_s))
    # `move_on_after` no lanza: si vencio el plazo, sale del bloque sin token.
    # El `acquire` de anyio es cancel-safe (si lo despiertan justo al vencer,
    # devuelve el token), asi que no se filtran turnos.
    with anyio.move_on_after(espera) as scope:
        await _normalize_slots.acquire()
    if scope.cancel_called:
        reintento = max(1, math.ceil(espera))
        stage(logger, "HTTP", "429 servicio saturado", job=job_id,
              en_curso=_normalize_in_flight, cupo=CFG.max_concurrent_normalize,
              espero_s=espera)
        raise HTTPException(
            429,
            f"el servicio esta procesando {CFG.max_concurrent_normalize} archivos y no "
            f"se libero un turno en {espera:.0f}s. Reintenta en unos segundos.",
            headers={"Retry-After": str(reintento)},
        )
    _normalize_in_flight += 1
    try:
        yield
    finally:
        _normalize_in_flight -= 1
        _normalize_slots.release()


def _city_centroids_ready() -> bool:
    """True si cities15000 esta disponible (centroide + pais por ciudad)."""
    from ..geocoding.city_lookup import lookup_city_centroid
    return lookup_city_centroid("Buenos Aires", "AR") is not None


def _capability_guard(enabled: bool, name: str, hint: str) -> None:
    """503 con una explicacion util, en vez de un error raro mas adentro."""
    if not enabled:
        raise HTTPException(503, f"{name} esta deshabilitado en este despliegue. {hint}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stage(logger, "HTTP", "arrancando Smart Import", version="0.1.0",
          puerto=CFG.port, work_dir=CFG.work_dir)
    stage(logger, "HTTP", "capacidades",
          normalize="on",
          geocoding="on" if CFG.geocoding_enabled else "off",
          )

    if CFG.geocoding_enabled and not CFG.pbf_dir:
        stage(logger, "WARN", "geocoding habilitado pero SMART_IMPORT_PBF_DIR esta vacio: "
                              "no va a poder resolver ninguna direccion",
              level=logging.WARNING)

    # Se precalienta el snapshot de /health mientras corre el start_period del
    # healthcheck: la primera lectura parsea cities15000 entero, y no conviene
    # que ese costo lo pague el primer probe (o el primer usuario).
    await _environment()

    tareas = [asyncio.create_task(_purge_loop()),
              asyncio.create_task(_environment_loop())]
    try:
        yield
    finally:
        for tarea in tareas:
            tarea.cancel()


#: Cada cuanto se barren los jobs vencidos. Una hora alcanza: el TTL se mide en
#: horas y el barrido es una pasada sobre un dict.
PURGE_EVERY_S = 3600


async def _purge_loop() -> None:
    """Borra periodicamente los jobs terminados que ya nadie va a mirar.

    Sin esto el disco crece sin techo, y en este host lo comparte con el cutter.
    """
    ttl_s = float(CFG.job_ttl_hours) * 3600
    if ttl_s <= 0:
        stage(logger, "HTTP", "barrido de jobs desactivado (SMART_IMPORT_JOB_TTL_HOURS=0)")
        return
    while True:
        try:
            await asyncio.sleep(PURGE_EVERY_S)
            borrados = await run_in_threadpool(store.purge_older_than, ttl_s)
            if borrados:
                stage(logger, "HTTP", "jobs vencidos borrados",
                      cantidad=len(borrados), ttl_h=CFG.job_ttl_hours)
        except asyncio.CancelledError:
            raise
        except Exception:
            # El barrido no puede tumbar el servicio: se reintenta a la hora.
            logger.exception("fallo el barrido de jobs vencidos")


app = FastAPI(
    lifespan=lifespan,
    title="Vepathos Smart Import",
    version="0.1.0",
    description=(
        "Convierte archivos de entregas de cualquier formato al schema Vepathos. "
        "La geolocalizacion es una accion SEPARADA que decide el usuario: "
        "`normalize` nunca geocodifica."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(CFG.cors_origins),   # SMART_IMPORT_CORS_ORIGINS en produccion
    allow_methods=["*"],
    allow_headers=["*"],
)

#: Los archivos del job. Todo acceso a disco de los endpoints pasa por aca: es
#: la costura por la que, mas adelante, un nodo que no tiene el archivo lo pide.
artifacts = make_artifact_store(CFG)
#: El estado de los jobs: en memoria con `ROLE=embedded`, en Redis si no. El
#: store necesita los artefactos para que borrar un job se lleve sus archivos.
store = make_job_store(CFG, artifacts)

#: El puerto por el que los workers buscan y devuelven archivos. Se monta
#: siempre, pero solo responde con `SMART_IMPORT_WORKER_TOKEN` puesto: sin token
#: cada ruta contesta 404, igual que si no existiera. Montarlo segun el token de
#: arranque daria la misma superficie y ademas dejaria al router fuera del
#: alcance de los tests, que reemplazan la config despues de importar el modulo.
app.include_router(internal_router(lambda: _worker_ctx()))


def _schema_path(name: str) -> Path:
    try:
        return resolve_schema_path(SCHEMA_DIR, name)
    except SchemaNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


def _job_or_404(job_id: str) -> Job:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, f"job '{job_id}' inexistente")
    return job


# --------------------------------------------------------------- salud

#: Cada cuanto se vuelve a mirar el disco para /health. Lo que describe cambia
#: con un deploy o cuando termina de construirse un indice, no entre probes.
HEALTH_SCAN_TTL_S = 30.0
_health_scan: tuple[float, dict[str, Any]] | None = None
_health_scan_lock = anyio.Lock()


def _scan_environment() -> dict[str, Any]:
    """La parte de /health que toca disco (se cachea, ver `_environment`)."""
    from ..addresses import describe_parsers
    from ..geocoding.extract import scan_registry
    from ..geocoding.pbf_registry import PbfRegistry

    cfg = CFG
    registry = (scan_registry(cfg.pbf_dir, cfg.extract_dir)
                if cfg.geocoding_enabled and cfg.pbf_dir else PbfRegistry([]))
    try:
        import phonenumbers                     # noqa: F401
        phones_ready = True
    except ImportError:
        phones_ready = False

    return {
        "schemas": sorted(p.stem for p in SCHEMA_DIR.glob("*.json")),
        "pbf_available": len(registry.entries),
        "indexes": (sorted(p.name for p in Path(cfg.index_dir).glob("*.sqlite"))
                    if Path(cfg.index_dir).exists() else []),
        "phonenumbers": phones_ready,
        "parsers": describe_parsers(cfg),
        "city_centroids": _city_centroids_ready(),
    }


async def _refresh_environment() -> dict[str, Any]:
    global _health_scan
    data = await run_in_threadpool(_scan_environment)
    _health_scan = (time.monotonic(), data)
    return data


async def _environment() -> dict[str, Any]:
    """El ultimo snapshot conocido. No escanea si ya hay uno.

    Refrescar dentro del request era el ultimo lugar donde /health se podia
    trabar: medido bajo 12 uploads, el probe daba p50 30 ms pero cada vez que
    vencia el TTL uno se comia entero el timeout de 5 s, porque el escaneo
    peleaba el GIL contra los normalize. Del refresco se encarga
    `_environment_loop`; aca solo se lee lo que haya.
    """
    snap = _health_scan
    if snap is not None:
        return snap[1]
    async with _health_scan_lock:
        if _health_scan is not None:     # otro probe lo cargo mientras esperaba
            return _health_scan[1]
        return await _refresh_environment()


async def _environment_loop() -> None:
    """Mantiene fresco el snapshot de /health fuera del camino del request."""
    while True:
        try:
            await asyncio.sleep(HEALTH_SCAN_TTL_S)
            await _refresh_environment()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Se sigue sirviendo el snapshot anterior; `environment_age_s` en
            # /health delata que quedo viejo.
            logger.exception("fallo el refresco del snapshot de /health")


@app.get("/health", tags=["meta"])
async def health() -> dict[str, Any]:
    """Que puede hacer este servicio ahora mismo.

    `async` y cacheado a proposito. Este es el probe del healthcheck de Docker,
    y como `def` sync competia por el mismo threadpool que el normalize: medido,
    bajo 12 uploads simultaneos tardaba 10.8 s, muy por encima del timeout de 5 s
    del probe. Tres de esos seguidos marcan el container unhealthy, lo reinician
    y se pierden todos los jobs (el store vive en memoria). O sea: el healthcheck
    reportaba la carga como si fuera una falla, y la convertia en una.
    """
    cfg = CFG
    env = await _environment()
    parsers = env["parsers"]
    return {
        "status": "ok",
        "version": app.version,
        "limits": {"max_file_mb": cfg.max_file_mb, "max_rows": cfg.max_rows},
        "schemas": env["schemas"],
        # Que tan lleno esta el servicio ahora. `normalize_in_flight` pegado al
        # cupo significa que los clientes nuevos estan esperando turno o
        # recibiendo 429: es la senal para subir el cupo o agregar una instancia.
        "load": {
            "normalize_in_flight": _normalize_in_flight,
            "normalize_slots": cfg.max_concurrent_normalize,
            "normalize_queue_wait_s": cfg.normalize_queue_wait_s,
            # Admitidos = subiendo + esperando turno + normalizando. Cuando
            # toca el cupo, los imports nuevos se rechazan antes de leer el
            # archivo: es la senal de que hace falta otra instancia.
            "admitted": _normalize_admitted,
            "admission_slots": cfg.max_normalize_queue,
            "geocode_slots": max(1, cfg.geocode_workers),
        },
        "capabilities": {
            "normalize": True,                        # siempre; es el nucleo
            "geocoding": cfg.geocoding_enabled,
            # el motor real de extraccion: reglas + librerias, sin modelo
            "rules": True,
            "phonenumbers": env["phonenumbers"],
            "libpostal": parsers["libpostal_installed"] and parsers["libpostal_enabled"],
        },
        "extraction": {
            "engine": "rules",
            "address_parser": parsers["active"],
            "libpostal_installed": parsers["libpostal_installed"],
            "libpostal_loaded": parsers.get("libpostal_loaded", False),
            "libpostal_enabled": parsers["libpostal_enabled"],
            "libpostal_as_enhancer": parsers.get("libpostal_as_enhancer", False),
            "default_phone_region": cfg.default_phone_region or None,
            "thresholds": {
                "delivery_accept": cfg.delivery_accept_threshold,
                "delivery_review": cfg.delivery_review_threshold,
                "address_accept": cfg.address_accept_threshold,
            },
            # Cuanta evidencia hace falta para reclamar una columna, por nivel
            # del schema: el destino pide mas que el bulto.
            "mapping_floor": {
                level: round(cfg.mapping_floor(level), 3)
                for level in ("delivery", "timewindow", "package")
            },
        },
        "geocoding": {
            "enabled": cfg.geocoding_enabled,
            "pbf_dir": cfg.pbf_dir or None,
            "pbf_available": env["pbf_available"],
            "indexes_built": env["indexes"],
            "autobuild_index": cfg.autobuild_index,
            "autoextract": cfg.autoextract,
            "extract_dir": cfg.extract_dir or None,
            "fallback": cfg.geocoder_fallback,
            "automatic": False,        # NUNCA se dispara solo
            # cities15000 de GeoNames. Si es false, "la ciudad manda sobre el
            # pin del depot" y la deteccion por encabezado quedan APAGADAS en
            # silencio: el build lo baja en una cache que no queda en la imagen.
            "city_centroids": env["city_centroids"],
        },
        "jobs": len(store),
        # Que rol quedo aplicado y donde vive el estado. Es lo que se mira
        # despues de un deploy para confirmar que el .env se leyo: `embedded`
        # con memoria es un solo proceso, `api` con redis es el estado
        # compartido con los workers. Sale del store REAL, no de la config, que
        # es la diferencia entre verificar y creerle al archivo.
        "deployment": {
            "role": cfg.role,
            "state": "redis" if type(store).__name__ == "RedisJobStore" else "memory",
        },
        # Antiguedad del snapshot de disco. Si crece mucho por encima de
        # HEALTH_SCAN_TTL_S, el refresco de fondo murio y lo de arriba es viejo.
        "environment_age_s": round(time.monotonic() - _health_scan[0], 1),
    }


@app.get("/config", tags=["meta"])
def effective_config() -> dict[str, Any]:
    """Config efectiva del proceso. Util para verificar que el .env se aplico."""
    return {"config": CFG.describe()}


@app.get("/schemas", tags=["meta"])
def list_schemas() -> dict[str, Any]:
    """Schemas destino disponibles y sus campos."""
    out = []
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        schema = TargetSchema.load(path)
        out.append({
            "name": schema.name,
            "columns": list(schema.column_order),
            "group_by": schema.group_by,
            "fields": {n: {"type": f.type, "level": f.level, "aliases": list(f.aliases)[:6]}
                       for n, f in schema.fields.items()},
        })
    return {"schemas": out}


# --------------------------------------------------------------- import

@app.post("/imports", tags=["import"], status_code=201)
async def create_import(
    file: UploadFile = File(..., description="CSV, TSV, TXT, XLSX, XLS o JSON"),
    schema: str = Query("vepathos_flat_v1"),
    phone_region: str | None = Query(None, description="ISO de region para telefonos: AR, US, IN"),
    timezone: str | None = Query(
        None,
        description="IANA TZ de settings del usuario (ej. America/Argentina/Buenos_Aires). "
                    "Si viene, las ventanas se interpretan en esa zona y se emiten en UTC.",
    ),
    depot_timezone: str | None = Query(
        None, description="IANA TZ del depot (fallback si no hay timezone de settings).",
    ),
    service_date: date | None = Query(
        None, description="Dia de servicio para ventanas horarias (default: manana).",
    ),
    depot_city: str | None = Query(None, description="Ciudad del depot (enriquece address)"),
    depot_region: str | None = Query(None),
    depot_country: str | None = Query(
        None, description="Pais del depot. Gana sobre phone_region para el pais de las direcciones.",
    ),
    diagnostics: bool = Query(False, description="Agregar columnas row_status/row_issues"),
) -> dict[str, Any]:
    """Sube un archivo y lo normaliza al formato Vepathos.

    NO geocodifica. Si faltan coordenadas, la respuesta lo dice en
    `report.needs_geocode` y ofrece la accion `geocode` en `next_actions`.
    """
    cfg = Config.from_env()
    name = safe_filename(file.filename)
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(415, f"extension '{suffix}' no soportada. "
                                 f"Permitidas: {sorted(ALLOWED_SUFFIXES)}")

    # Primero la admision, ANTES de leer una sola linea del body: recien
    # despues se crea el job y se escribe en disco. Al reves, una avalancha de
    # 1000 uploads deja hasta 1000 x max_file_mb en el disco que compartimos con
    # el cutter antes de que alguien rechace nada.
    async with _admitted():
        schema_path = _schema_path(schema)
        job = store.create(name, schema)
        job.capabilities = {"geocoding": CFG.geocoding_enabled and bool(CFG.pbf_dir),
                            }
        raw_path = artifacts.reserve(job.id, RAW, filename=name)

        size = 0
        limit = int(cfg.max_file_mb * 1024 * 1024)
        try:
            with open(raw_path, "wb") as fh:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:             # se corta al vuelo, no se lee entero
                        raise HTTPException(413, f"el archivo supera {cfg.max_file_mb} MB")
                    fh.write(chunk)
        except HTTPException:
            store.delete(job.id)
            raise
        finally:
            await file.close()

        job.raw_path = artifacts.publish(job.id, RAW, raw_path)
        store.save(job)
        stage(logger, "HTTP", "POST /imports", job=job.id, archivo=name,
              tamano=f"{size / 1024:.1f}KB", schema=schema)
        # El normalize es CPU-bound (lectura, mapping, extraccion, libpostal) y
        # este endpoint es una corrutina: ejecutarlo inline bloquea el event loop
        # y con el loop bloqueado no se atiende NADA — ni /health (el healthcheck
        # marca el container unhealthy) ni los SSE de progreso ni el polling.
        # El upload de arriba si es async de verdad: `await file.read()` cede.
        try:
            async with _normalize_slot(job.id):
                response = await run_in_threadpool(
                    _run_normalize,
                    job, schema_path, phone_region, diagnostics,
                    timezone=timezone, depot_timezone=depot_timezone,
                    service_date=service_date,
                    depot_city=depot_city, depot_region=depot_region,
                    depot_country=depot_country,
                )
        except HTTPException as exc:
            # Un job rechazado por saturacion no lo va a ver nadie: si se queda,
            # cada reintento del front deja otra copia del archivo en disco hasta
            # que pase el TTL. Un normalize que falla (422) si se conserva: el
            # usuario necesita leer el error.
            if exc.status_code == 429:
                store.delete(job.id)
            raise
    stage(logger, "HTTP", "201 creado", job=job.id, estado=job.status,
          acciones=",".join(a["action"] for a in job.next_actions()))
    return response


def _worker_ctx() -> WorkerContext:
    """Lo que antes eran los globals de este modulo, ahora explicito.

    Se arma por llamada a proposito: los tests reemplazan `CFG` con
    `monkeypatch.setattr(api_module, "CFG", cfg)` y un contexto cacheado se
    quedaria con la config vieja.
    """
    return WorkerContext(cfg=CFG, store=store, artifacts=artifacts, logger=logger)


def _run_normalize(job: Job, schema_path: Path, phone_region: str | None,
                   diagnostics: bool, manual_mapping: dict | None = None,
                   timezone: str | None = None,
                   depot_timezone: str | None = None,
                   service_date: date | None = None,
                   depot_city: str | None = None,
                   depot_region: str | None = None,
                   depot_country: str | None = None) -> dict[str, Any]:
    """Adapta el handler al transporte HTTP: la falla de dominio sale 422."""
    try:
        return run_normalize_job(
            _worker_ctx(), job, schema_path, phone_region, diagnostics,
            manual_mapping=manual_mapping, timezone=timezone,
            depot_timezone=depot_timezone, service_date=service_date,
            depot_city=depot_city, depot_region=depot_region,
            depot_country=depot_country)
    except NormalizeFailed as exc:
        raise HTTPException(422, f"no se pudo procesar el archivo: {exc}") from exc


@app.get("/imports", tags=["import"])
def list_imports(limit: int = Query(50, le=500)) -> dict[str, Any]:
    return {"jobs": [j.as_dict() for j in store.list(limit)]}


@app.get("/imports/{job_id}", tags=["import"])
def get_import(job_id: str) -> dict[str, Any]:
    """Estado actual del job. La web puede pollear esto cada `poll_after_ms`."""
    return _job_or_404(job_id).as_dict()


@app.get("/imports/{job_id}/progress", tags=["import"])
def get_progress(job_id: str) -> dict[str, Any]:
    """Solo el snapshot de avance (payload chico para polling frecuente)."""
    return _job_or_404(job_id).progress_snapshot()


@app.get("/imports/{job_id}/events", tags=["import"])
async def import_events(
    job_id: str,
    interval_ms: int = Query(500, ge=200, le=5000,
                             description="Cada cuantos ms empujar un evento si hay cambio"),
) -> StreamingResponse:
    """Server-Sent Events con el avance del job.

    Eventos:
      - `progress`  → snapshot `{status, phase, done, total, pct, eta_s, busy, …}`
      - `done`      → job completo (busy=false); data = as_dict()
      - `error`     → job borrado / inexistente

    Uso desde la web:
      const es = new EventSource(`${SMART_IMPORT_URL}/imports/${jobId}/events`)
      es.addEventListener('progress', e => setBar(JSON.parse(e.data)))
      es.addEventListener('done', e => { setJob(JSON.parse(e.data)); es.close() })
    """
    _job_or_404(job_id)
    sleep_s = interval_ms / 1000.0

    async def generate():
        last: str | None = None
        # primer evento inmediato
        while True:
            job = store.get(job_id)
            if job is None:
                yield f"event: error\ndata: {json.dumps({'error': 'job inexistente'})}\n\n"
                return
            snap = job.progress_snapshot()
            payload = json.dumps(snap, ensure_ascii=False)
            if payload != last:
                yield f"event: progress\ndata: {payload}\n\n"
                last = payload
            if not job.busy:
                yield f"event: done\ndata: {json.dumps(job.as_dict(), ensure_ascii=False)}\n\n"
                return
            await asyncio.sleep(sleep_s)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/imports/{job_id}/preview", tags=["import"])
def preview(job_id: str, limit: int = Query(20, le=200),
            source: str = Query("auto",
                                pattern="^(auto|normalized|geocoded)$")) -> dict[str, Any]:
    """Muestra de las filas resultantes, para que la UI las pinte antes de importar.

    `auto` (default) devuelve el resultado VIGENTE: el geocodificado si ya existe.
    Con el default anterior (`normalized`) la UI recibia el CSV previo al geocode,
    sin `geocode_band` ni confianza, y pintaba todas las filas iguales.
    """
    import csv

    job = _job_or_404(job_id)
    if source == "auto":
        kind, ref = _flat_artifact(job)
        source = "geocoded" if kind == GEOCODED else "normalized"
    elif source == "geocoded":
        kind, ref = GEOCODED, job.geocoded_path
    else:
        kind, ref = FLAT, job.normalized_path
    path = artifacts.resolve(job.id, kind, ref)
    if path is None:
        raise HTTPException(409, f"el job todavia no tiene resultado '{source}'")

    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = [row for _, row in zip(range(limit), reader)]
        columns = reader.fieldnames or []
    return {"job_id": job_id, "source": source, "columns": columns,
            "rows": rows, "mapping": job.report.get("mapping", {})}


@app.get("/imports/{job_id}/issues", tags=["import"])
def issues(job_id: str, limit: int = Query(500, le=2000)) -> dict[str, Any]:
    """Las filas que NO quedaron listas, con el motivo y la columna culpable.

    Ninguna fila se descarta: todas estan en el archivo de salida. Este endpoint
    existe para que la UI pueda mostrar "3 listas, 3 a geocodificar, 1 con
    problema" en vez de perder filas en silencio.

    Despues del geocode, las filas sin coordenadas (not_found / sin match) se
    listan como `a_geocodificar` para que el usuario las ubique a mano.
    """
    from ..geocoding.bands import (
        BAND_NEEDS_GEOCODING, BAND_REVIEW, BAND_VALID, band_from_row, percent,
    )

    job = _job_or_404(job_id)
    report = job.report or {}
    if not report:
        raise HTTPException(409, "el job todavia no fue normalizado")

    filas = list(report.get("row_issues", []) or [])
    geo = job.geocode_report or {}

    # Despues del geocode se listan las filas que el operador tiene que TOCAR:
    # las que no tienen pin (ubicar a mano) y las de la banda de revision (hay
    # pin, pero es a nivel calle). Antes del geocode, los row_issues del normalize.
    pending_manual: list[dict[str, Any]] = []
    por_banda = {BAND_VALID: 0, BAND_REVIEW: 0, BAND_NEEDS_GEOCODING: 0}
    if geo:
        src_path = (artifacts.resolve(job.id, GEOCODED, job.geocoded_path)
                    or artifacts.resolve(job.id, FLAT, job.normalized_path))
        if src_path and src_path.suffix.lower() == ".csv":
            import csv as _csv
            bandas = (CFG.geocode_valid_band, CFG.geocode_review_band)
            with open(src_path, encoding="utf-8", newline="") as fh:
                for i, row in enumerate(_csv.DictReader(fh), start=1):
                    address = (row.get("address") or "").strip()
                    if not address and not (row.get("delivery_id") or "").strip():
                        continue
                    # Siempre recalcular: un CSV stamped con reglas viejas no
                    # puede pintar Review 87% cuando VALID_BAND=0.85.
                    banda = band_from_row(
                        row, valid_at=bandas[0], review_at=bandas[1])
                    por_banda[banda] = por_banda.get(banda, 0) + 1
                    if banda == BAND_VALID:
                        continue

                    status = (row.get("geocode_status") or "").strip() or "needs_geocode"
                    try:
                        confianza = float(row.get("geocode_confidence") or "") or None
                    except (TypeError, ValueError):
                        confianza = None
                    try:
                        raw_score = float(row.get("geocode_raw_score") or "") or None
                    except (TypeError, ValueError):
                        raw_score = None
                    reason = (row.get("geocode_reason") or "").strip() or None
                    show_pct = percent(confianza if confianza is not None else raw_score)
                    manual = banda == BAND_NEEDS_GEOCODING
                    pending_manual.append({
                        "row": i,
                        "delivery_id": row.get("delivery_id") or None,
                        "status": "a_geocodificar" if manual else "a_revisar",
                        "geocode_band": banda,
                        "geocode_status": status,
                        "geocode_confidence": confianza,
                        "geocode_raw_score": raw_score,
                        "geocode_reason": reason,
                        "geocode_percent": show_pct,
                        "geocode_precision": (row.get("geocode_precision") or "") or None,
                        "fields": ["address", "lat", "lng"],
                        "messages": [
                            address or "(sin direccion)",
                            f"geocode={status}"
                            + (f" ({show_pct}%)" if show_pct is not None else "")
                            + (f" · {reason}" if reason else ""),
                        ],
                    })
        if pending_manual:
            filas = pending_manual

    a_geocodificar = int(report.get("needs_geocode", 0) or 0)
    a_revisar = 0
    if geo:
        # "listas" = solo la banda verde. Una fila ambar tiene pin pero NO esta
        # lista: contarla como lista es lo que hacia que nadie la revisara.
        listas = por_banda.get(BAND_VALID, 0) or int(geo.get("matched", 0) or 0)
        a_revisar = por_banda.get(BAND_REVIEW, 0)
        a_geocodificar = por_banda.get(BAND_NEEDS_GEOCODING, 0) or (
            int(geo.get("not_found", 0) or 0) + int(geo.get("errors", 0) or 0))
    else:
        listas = report.get("valid_rows", 0)
    return {
        "job_id": job_id,
        "summary": {
            "total": report.get("deliveries") or report.get("rows_output", 0),
            "listas": listas,
            "a_revisar": a_revisar,
            "a_geocodificar": a_geocodificar,
            "no_localizables": report.get("invalid_rows", 0),
            "ignoradas": report.get("ignored_rows", 0),
            "con_observaciones": report.get("rows_with_issues", 0),
            "coordenadas_rechazadas": report.get("rejected_coordinates", 0),
            **({"geocodificadas": geo.get("matched", 0),
                "geocode_confianza_baja": geo.get("low_confidence", 0),
                "geocode_no_encontradas": geo.get("not_found", 0),
                "bandas": por_banda,
                "umbrales_banda": {"valid": CFG.geocode_valid_band,
                                   "review": CFG.geocode_review_band}} if geo else {}),
        },
        "columnas_sin_mapear": report.get("unmapped", []),
        "columnas_a_revisar": report.get("ambiguous", []),
        "avisos": list(report.get("warnings") or []) + (
            [f"{a_geocodificar} entrega(s) sin coordenadas: ubicar a mano en el mapa"]
            if a_geocodificar else []
        ) + (
            [f"{a_revisar} entrega(s) resueltas a nivel calle: revisar el pin"]
            if a_revisar else []
        ),
        "filas": filas[:limit],
        "truncado": len(filas) > limit,
    }


@app.put("/imports/{job_id}/mapping", tags=["import"])
async def confirm_mapping(
    job_id: str,
    mapping: dict[str, str | None] = Body(..., examples=[{"Dest.": "address", "Obs": None}]),
    phone_region: str | None = Query(None),
    timezone: str | None = Query(None),
    depot_timezone: str | None = Query(None),
    service_date: date | None = Query(None),
    depot_city: str | None = Query(None),
    depot_region: str | None = Query(None),
    depot_country: str | None = Query(None),
    diagnostics: bool = Query(False),
) -> dict[str, Any]:
    """Corrige el mapping sugerido y vuelve a normalizar.

    Lo que decide el usuario es final: se marca `manual` y confianza 1.0.
    """
    job = _job_or_404(job_id)
    if not artifacts.exists(job.id, RAW, job.raw_path):
        raise HTTPException(409, "el archivo original ya no esta disponible")
    # Re-normalizar cuesta lo mismo que la primera vez, asi que pide turno igual:
    # si no, este endpoint es una puerta lateral que saltea el techo.
    async with _normalize_slot(job.id):
        return await run_in_threadpool(
            _run_normalize, job, _schema_path(job.schema), phone_region, diagnostics,
            manual_mapping=mapping, timezone=timezone,
            depot_timezone=depot_timezone, service_date=service_date,
            depot_city=depot_city, depot_region=depot_region,
            depot_country=depot_country)


def _flat_artifact(job: Job) -> tuple[str, str | None]:
    """El CSV plano MAS ACTUAL del job, como (kind, referencia).

    Despues de geocodificar, el resultado vigente es el CSV geocodificado: mismas
    columnas del schema MAS `lat/lng` y los `geocode_*` (superset estricto). Servir
    el de antes del geocode deja al cliente sin `geocode_band` ni confianza y le
    hace pintar todo igual — que es exactamente el sintoma que trajo este arreglo.
    """
    if artifacts.exists(job.id, GEOCODED, job.geocoded_path):
        return GEOCODED, job.geocoded_path
    return FLAT, job.normalized_path


def _nested_artifact(job: Job) -> tuple[str, str | None]:
    """El JSON anidado vigente.

    Tras el geocode, `refresh_nested` lo regenera dentro de `geocoded/`: es otro
    artefacto, aunque el `Job` lo guarde en el mismo campo.
    """
    kind = (GEOCODED_NESTED if artifacts.exists(job.id, GEOCODED, job.geocoded_path)
            else NESTED)
    return kind, job.nested_path


@app.get("/imports/{job_id}/download", tags=["import"])
def download(job_id: str,
             format: str = Query("flat",
                                 pattern="^(flat|nested|geocoded|normalized)$")) -> FileResponse:
    """Descarga el resultado.

    `flat` = el CSV Vepathos vigente (el geocodificado si ya se geocodifico).
    `normalized` fuerza el previo al geocode; `geocoded` exige que exista.
    """
    job = _job_or_404(job_id)
    kind, ref = {"flat": _flat_artifact(job),
                 "nested": _nested_artifact(job),
                 "normalized": (FLAT, job.normalized_path),
                 "geocoded": (GEOCODED, job.geocoded_path)}[format]
    path = artifacts.resolve(job.id, kind, ref)
    if path is None:
        raise HTTPException(409, f"el job no tiene salida '{format}' todavia")
    media = "application/json" if format == "nested" else "text/csv"
    return FileResponse(path, media_type=media, filename=Path(path).name)


@app.delete("/imports/{job_id}", tags=["import"], status_code=204)
def delete_import(job_id: str) -> None:
    if not store.delete(job_id):
        raise HTTPException(404, f"job '{job_id}' inexistente")


# --------------------------------------------------------------- geocode

@app.post("/imports/{job_id}/geocode", tags=["geocode"], status_code=202)
def start_geocode(
    job_id: str,
    origin_lat: float | None = Query(None, description="Depot: sesga y desempata homonimos"),
    origin_lon: float | None = Query(None),
    bbox: str | None = Query(None, description="north,south,east,west"),
    index: str | None = Query(None, description="Nombre del indice .sqlite a forzar"),
    depot_city: str | None = Query(None, description="Ciudad/comuna del depot (enrichment)"),
    depot_region: str | None = Query(None, description="Provincia/estado del depot"),
    depot_postcode: str | None = Query(None, description="Codigo postal del depot"),
    depot_country: str | None = Query(None, description="Pais del depot"),
    depot_address: str | None = Query(
        None, description="Direccion libre del depot (se parte por comas)"),
    max_distance_km: float | None = Query(
        None, description="Geofence duro en km (default GEOCODE_MAX_DISTANCE_KM=500)"),
    enhance_addresses: bool = Query(
        False,
        description=(
            "Opt-in: reescribe la QUERY de geocode con el parser/enhance "
            "(road+altura). El address visible del usuario NO se modifica. "
            "Pensado para el switch 'mejorar direcciones' del modal de geocode."
        ),
    ),
) -> dict[str, Any]:
    """Arranca la geolocalizacion de las filas que NO tienen coordenadas.

    Es una accion explicita: el pipeline nunca la dispara por su cuenta. Las
    filas que ya traian coordenadas se conservan intactas.

    Por defecto geocodifica con el address cargado (+ ciudad/region de la fila
    y del depot solo en la query interna). Con `enhance_addresses=true` la query
    se limpia/reescribe; el texto en tabla sigue siendo el del usuario.
    """
    _capability_guard(CFG.geocoding_enabled, "El geocoding",
                      "Habilitalo con SMART_IMPORT_GEOCODING_ENABLED=true.")
    _capability_guard(bool(CFG.pbf_dir), "El geocoding",
                      "Falta SMART_IMPORT_PBF_DIR apuntando a los .osm.pbf.")
    job = _job_or_404(job_id)
    if job.busy:
        # Chequeo temprano para no hacer trabajo al pedo; el que decide de
        # verdad es el claim atomico de mas abajo.
        raise HTTPException(409, "ya hay una geolocalizacion en curso para este job")
    if not artifacts.exists(job.id, FLAT, job.normalized_path):
        raise HTTPException(409, "hay que normalizar el archivo antes de geocodificar")

    box = None
    if bbox:
        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) != 4:
            raise HTTPException(400, "bbox espera north,south,east,west")
        box = tuple(float(p) for p in parts)

    origin = (origin_lat, origin_lon) if origin_lat is not None and origin_lon is not None else None
    if origin is None and box is None and index is None:
        # Sin depot todavia se puede geocodificar: basta la ciudad que el
        # usuario confirmo (o que se detecto en el archivo, ver
        # report.locality). El centroide de la ciudad hace de origin y elige
        # la region de OSM; `align_depot_to_geolocator` lo aplica en el worker.
        from ..geocoding.city_lookup import lookup_city_centroid
        ciudad = (depot_city or "").strip()
        if not ciudad:
            raise HTTPException(400, "indica la ciudad (depot_city), el depot (origin_lat/lon), "
                                     "un bbox o un indice: sin eso no se puede elegir que "
                                     "region de OSM usar")
        if lookup_city_centroid(ciudad, depot_country) is None:
            raise HTTPException(
                400,
                f"no pude ubicar '{ciudad}'"
                + (f" en {depot_country}" if depot_country else "")
                + ": mandá el depot (origin_lat/lon) o corregí la ciudad")

    from ..geocoding.depot_context import depot_from_params
    depot = depot_from_params(
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        depot_city=depot_city,
        depot_region=depot_region,
        depot_postcode=depot_postcode,
        depot_country=depot_country,
        depot_address=depot_address,
        max_distance_km=(
            float(max_distance_km)
            if max_distance_km is not None
            else float(CFG.max_geocode_distance_km)
        ),
    )

    total = int(job.report.get("rows_output") or job.needs_geocode or 0)
    # Reserva atomica: si otro request se adelanto, este no lanza un segundo
    # worker sobre el mismo CSV.
    if not store.claim_geocode(job.id):
        raise HTTPException(409, "ya hay una geolocalizacion en curso para este job")
    # El claim escribio el estado EN EL STORE. Lo que hay en `job` es de antes,
    # asi que hay que releer: guardar la version vieja pisaria la reserva y el
    # job volveria a estar libre para un segundo worker.
    job = _job_or_404(job_id)
    stage(logger, "HTTP", "POST /imports/{id}/geocode  (accion EXPLICITA del usuario)",
          job=job_id, depot=f"{origin_lat},{origin_lon}" if origin else None,
          enrich=depot.enrichment_tokens() if depot else None,
          enhance_addresses=bool(enhance_addresses) or None,
          filas_a_geocodificar=job.needs_geocode)
    job.op_started_at = time.time()
    job.geocode_progress = {"phase": "queued", "done": 0, "total": total}
    job.geocode_report = {}
    store.save(job)
    _geocode_pool.submit(
        _geocode_worker, job.id, origin, box, index, depot, enhance_addresses)
    return {**job.as_dict(),
            "poll": f"/imports/{job_id}",
            "events": f"/imports/{job_id}/events",
            "geocode_options": {
                "enhance_addresses": bool(enhance_addresses),
                "note": ("Por defecto se geocodifica el address cargado "
                         "(query + city/depot internos). "
                         "enhance_addresses=true mejora solo la query."),
            }}


def _geocode_worker(job_id: str, origin, box, index_name: str | None,
                    depot=None, enhance_addresses: bool = False) -> None:
    run_geocode_job(_worker_ctx(), job_id, origin, box, index_name,
                    depot=depot, enhance_addresses=enhance_addresses)


def _refresh_nested(job: Job, geocoded_csv: Path) -> None:
    refresh_nested(_worker_ctx(), job, geocoded_csv)

@app.get("/geocoding/coverage", tags=["geocode"])
def coverage(lat: float = Query(...), lon: float = Query(...)) -> dict[str, Any]:
    """Hay cobertura OSM para este punto? Sirve para decidir ANTES de ofrecer el boton."""
    from ..geocoding.extract import scan_registry
    from ..geocoding.osm_index import covering_extract_index, index_path_for
    from ..geocoding.pbf_registry import PbfRegistry

    cfg = CFG
    if not cfg.geocoding_enabled:
        return {"available": False, "point": [lat, lon],
                "reason": "el geocoding esta deshabilitado en este despliegue"}
    registry = (scan_registry(cfg.pbf_dir, cfg.extract_dir)
                if cfg.pbf_dir else PbfRegistry([]))
    entry = registry.resolve(lat=lat, lon=lon)
    if entry is None:
        return {"available": False, "point": [lat, lon],
                "reason": "ningun .osm.pbf cubre ese punto",
                "pbf_scanned": len(registry.entries)}
    leftover = covering_extract_index(cfg.index_dir, lat, lon)
    index_path = leftover or index_path_for(entry, cfg.index_dir)
    will_cut = (
        leftover is None and not entry.has_bbox and cfg.autoextract
        and not index_path.exists()
    )
    return {
        "available": True, "point": [lat, lon],
        "pbf": entry.as_dict(),
        "index_ready": index_path.exists(),
        "index": str(index_path),
        "will_cut_extract": will_cut,
        "note": ("el indice ya esta construido" if index_path.exists()
                 else ("la primera geolocalizacion corta un extract de ciudad "
                       "y construye el indice (no indexa el PBF de pais)"
                       if will_cut
                       else "la primera geolocalizacion en esta zona construye el indice "
                            "(una sola vez, ~10 s por cada 25 MB de PBF)")),
    }



