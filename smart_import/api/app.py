"""API HTTP de Smart Import.

Diseno:
  - `normalize` es SINCRONO: 50k filas tardan ~1.5 s, no justifica una cola.
  - `geocode` es ASINCRONO y SIEMPRE explicito: nunca se dispara solo.
  - el archivo subido no viaja mas alla de esta capa; se guarda en disco y el
    resto del pipeline trabaja con rutas (misma forma que tendra con RabbitMQ).
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..config import Config
from ..schemas import TargetSchema
from .jobs import (
    ALLOWED_SUFFIXES, COMPLETED, FAILED, GEOCODE_FAILED,
    GEOCODE_QUEUED, GEOCODING, NEEDS_REVIEW, NORMALIZED, Job, JobStore,
    safe_filename,
)

from ..logging_setup import get_logger, setup as setup_logging, stage

CFG = Config.from_env()
setup_logging(verbose=CFG.verbose)
logger = get_logger("api")

# El directorio de schemas es configurable: en el container vive en /app/schemas,
# en desarrollo en ./schemas, y un cliente puede montar los suyos.
SCHEMA_DIR = Path(CFG.schema_dir)
if not SCHEMA_DIR.is_absolute() and not SCHEMA_DIR.exists():
    SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / CFG.schema_dir

# Un worker por defecto para cada tarea pesada: este container comparte la VM con
# el cutter y no tiene que competirle CPU. Configurable cuando haya medicion.
_geocode_pool = ThreadPoolExecutor(max_workers=max(1, CFG.geocode_workers),
                                   thread_name_prefix="geocode")


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

    yield


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

store = JobStore(CFG.work_dir)


def _schema_path(name: str) -> Path:
    path = SCHEMA_DIR / f"{name}.json"
    if not path.exists():
        raise HTTPException(404, f"schema '{name}' inexistente")
    return path


def _job_or_404(job_id: str) -> Job:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, f"job '{job_id}' inexistente")
    return job


# --------------------------------------------------------------- salud

@app.get("/health", tags=["meta"])
def health() -> dict[str, Any]:
    """Que puede hacer este servicio ahora mismo."""
    from ..geocoding.extract import scan_registry
    from ..geocoding.pbf_registry import PbfRegistry

    cfg = CFG
    registry = (scan_registry(cfg.pbf_dir, cfg.extract_dir)
                if cfg.geocoding_enabled and cfg.pbf_dir else PbfRegistry([]))
    indexes = sorted(p.name for p in Path(cfg.index_dir).glob("*.sqlite")) \
        if Path(cfg.index_dir).exists() else []

    try:
        import phonenumbers                     # noqa: F401
        phones_ready = True
    except ImportError:
        phones_ready = False

    from ..addresses import describe_parsers
    parsers = describe_parsers(cfg)

    return {
        "status": "ok",
        "version": app.version,
        "limits": {"max_file_mb": cfg.max_file_mb, "max_rows": cfg.max_rows},
        "schemas": sorted(p.stem for p in SCHEMA_DIR.glob("*.json")),
        "capabilities": {
            "normalize": True,                        # siempre; es el nucleo
            "geocoding": cfg.geocoding_enabled,
            # el motor real de extraccion: reglas + librerias, sin modelo
            "rules": True,
            "phonenumbers": phones_ready,
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
        },
        "geocoding": {
            "enabled": cfg.geocoding_enabled,
            "pbf_dir": cfg.pbf_dir or None,
            "pbf_available": len(registry.entries),
            "indexes_built": indexes,
            "autobuild_index": cfg.autobuild_index,
            "autoextract": cfg.autoextract,
            "extract_dir": cfg.extract_dir or None,
            "fallback": cfg.geocoder_fallback,
            "automatic": False,        # NUNCA se dispara solo
        },
        "jobs": len(store.list(limit=10_000)),
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

    schema_path = _schema_path(schema)
    job = store.create(name, schema)
    job.capabilities = {"geocoding": CFG.geocoding_enabled and bool(CFG.pbf_dir),
                        }
    raw_path = store.dir_for(job.id) / "raw" / name

    size = 0
    limit = int(cfg.max_file_mb * 1024 * 1024)
    try:
        with open(raw_path, "wb") as fh:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:                 # se corta al vuelo, no se lee entero
                    raise HTTPException(413, f"el archivo supera {cfg.max_file_mb} MB")
                fh.write(chunk)
    except HTTPException:
        store.delete(job.id)
        raise
    finally:
        await file.close()

    job.raw_path = str(raw_path)
    stage(logger, "HTTP", "POST /imports", job=job.id, archivo=name,
          tamano=f"{size / 1024:.1f}KB", schema=schema)
    # El normalize es CPU-bound (lectura, mapping, extraccion, libpostal) y este
    # endpoint es una corrutina: ejecutarlo inline bloquea el event loop y con el
    # loop bloqueado no se atiende NADA — ni /health (el healthcheck marca el
    # container unhealthy) ni los SSE de progreso ni el polling del front.
    # El upload de arriba si es async de verdad: `await file.read()` cede.
    response = await run_in_threadpool(
        _run_normalize,
        job, schema_path, phone_region, diagnostics,
        timezone=timezone, depot_timezone=depot_timezone,
        service_date=service_date,
        depot_city=depot_city, depot_region=depot_region,
        depot_country=depot_country,
    )
    stage(logger, "HTTP", "201 creado", job=job.id, estado=job.status,
          acciones=",".join(a["action"] for a in job.next_actions()))
    return response


def _run_normalize(job: Job, schema_path: Path, phone_region: str | None,
                   diagnostics: bool, manual_mapping: dict | None = None,
                   timezone: str | None = None,
                   depot_timezone: str | None = None,
                   service_date: date | None = None,
                   depot_city: str | None = None,
                   depot_region: str | None = None,
                   depot_country: str | None = None) -> dict[str, Any]:
    from ..extraction.tz import resolve_timezone
    from ..pipeline import run_normalize

    out_dir = store.dir_for(job.id) / "normalized"
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / "normalized.csv"
    resolved_tz = resolve_timezone(timezone, depot_timezone, job.timezone)

    job.touch(NORMALIZED)
    try:
        result = run_normalize(
            job.raw_path, schema_path, output, emit=("flat", "nested"),
            manual_mapping=manual_mapping, phone_region=phone_region,
            diagnostics=diagnostics, timezone=resolved_tz,
            service_date=service_date,
            depot_city=depot_city, depot_region=depot_region,
            depot_country=depot_country,
        )
    except Exception as exc:
        job.error = str(exc)
        job.touch(FAILED)
        logger.exception("normalize fallo en %s", job.id)
        raise HTTPException(422, f"no se pudo procesar el archivo: {exc}") from exc

    job.report = result.report
    job.normalized_path = result.outputs.get("flat")
    job.nested_path = result.outputs.get("nested")
    job.phone_region = phone_region
    job.timezone = resolved_tz
    job.touch(NEEDS_REVIEW if result.report.get("needs_review") else NORMALIZED)
    return job.as_dict()


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
        path = _flat_path(job)
        source = ("geocoded" if job.geocoded_path and path == job.geocoded_path
                  else "normalized")
    else:
        path = job.geocoded_path if source == "geocoded" else job.normalized_path
    if not path or not Path(path).exists():
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
        geo_path = Path(job.geocoded_path) if job.geocoded_path else None
        src_path = geo_path if geo_path and geo_path.exists() else (
            Path(job.normalized_path) if job.normalized_path else None
        )
        if src_path and src_path.exists() and src_path.suffix.lower() == ".csv":
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
def confirm_mapping(
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
    if not job.raw_path or not Path(job.raw_path).exists():
        raise HTTPException(409, "el archivo original ya no esta disponible")
    return _run_normalize(job, _schema_path(job.schema), phone_region, diagnostics,
                          manual_mapping=mapping, timezone=timezone,
                          depot_timezone=depot_timezone, service_date=service_date,
                          depot_city=depot_city, depot_region=depot_region,
                          depot_country=depot_country)


def _flat_path(job: Job) -> str | None:
    """El CSV plano MAS ACTUAL del job.

    Despues de geocodificar, el resultado vigente es el CSV geocodificado: mismas
    columnas del schema MAS `lat/lng` y los `geocode_*` (superset estricto). Servir
    el de antes del geocode deja al cliente sin `geocode_band` ni confianza y le
    hace pintar todo igual — que es exactamente el sintoma que trajo este arreglo.
    """
    geocoded = job.geocoded_path
    if geocoded and Path(geocoded).exists():
        return geocoded
    return job.normalized_path


@app.get("/imports/{job_id}/download", tags=["import"])
def download(job_id: str,
             format: str = Query("flat",
                                 pattern="^(flat|nested|geocoded|normalized)$")) -> FileResponse:
    """Descarga el resultado.

    `flat` = el CSV Vepathos vigente (el geocodificado si ya se geocodifico).
    `normalized` fuerza el previo al geocode; `geocoded` exige que exista.
    """
    job = _job_or_404(job_id)
    path = {"flat": _flat_path(job), "nested": job.nested_path,
            "normalized": job.normalized_path,
            "geocoded": job.geocoded_path}.get(format)
    if not path or not Path(path).exists():
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
    if not job.normalized_path or not Path(job.normalized_path).exists():
        raise HTTPException(409, "hay que normalizar el archivo antes de geocodificar")

    box = None
    if bbox:
        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) != 4:
            raise HTTPException(400, "bbox espera north,south,east,west")
        box = tuple(float(p) for p in parts)

    origin = (origin_lat, origin_lon) if origin_lat is not None and origin_lon is not None else None
    if origin is None and box is None and index is None:
        raise HTTPException(400, "indica --origin (depot), un bbox o un indice: sin eso no se "
                                 "puede elegir que region de OSM usar")

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
    stage(logger, "HTTP", "POST /imports/{id}/geocode  (accion EXPLICITA del usuario)",
          job=job_id, depot=f"{origin_lat},{origin_lon}" if origin else None,
          enrich=depot.enrichment_tokens() if depot else None,
          enhance_addresses=bool(enhance_addresses) or None,
          filas_a_geocodificar=job.needs_geocode)
    job.op_started_at = time.time()
    job.geocode_progress = {"phase": "queued", "done": 0, "total": total}
    job.geocode_report = {}
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
    from ..geocoding.extract import ExtractError, ensure_geocode_index_from_config
    from ..geocoding.locality import fill_depot_from_index
    from ..geocoding.osm_index import index_path_for
    from ..geocoding.runner import run

    job = store.get(job_id)
    if job is None:
        return
    cfg = CFG
    job.op_started_at = time.time()
    job.touch(GEOCODING)

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
                zone_hint = (depot.city or depot.region or depot.country or None)
                if zone_hint:
                    zone_hint = str(zone_hint).strip() or None

            def _index_progress(phase: str, pbf: str = "", **_kw) -> None:
                job.geocode_progress = {
                    "phase": phase, "pbf": pbf,
                    "done": 0, "total": int(job.report.get("rows_output") or 0),
                }
                job.touch()

            try:
                ready = ensure_geocode_index_from_config(
                    cfg, lat=lat, lon=lon, bbox=box, zone_hint=zone_hint,
                    progress=_index_progress,
                )
            except ExtractError as exc:
                raise FileNotFoundError(str(exc)) from exc
            index_path = ready.path
            country_slug = ready.country_slug
            stage(logger, "GEOCODE", "PBF elegido",
                  pbf=ready.entry.path.name, pais=ready.country_slug,
                  extract=ready.entry.key if ready.entry.has_bbox else None,
                  zone_hint=zone_hint,
                  cut=ready.cut_extract)

        # Ciudad/CP del depot desde el indice (Tandil, B7000, …) si el cliente
        # solo mando lat/lon. Sin esto enrich=[] y "Dufau 1418" no geocodifica.
        depot = fill_depot_from_index(depot, index_path, country_slug=country_slug)
        if depot and depot.enrichment_tokens():
            stage(logger, "GEOCODE", "contexto depot",
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

        out_dir = store.dir_for(job_id) / "geocoded"
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / "geocoded.csv"

        report = run(job.normalized_path, output, index_path, origin=origin, bbox=box,
                     config=cfg, progress=progress, depot=depot,
                     enhance_addresses=bool(enhance_addresses))
        job.geocoded_path = str(output)
        job.geocode_report = report.as_dict()

        # Actualizar needs_geocode residual: las not_found siguen pendientes
        # de ubicacion manual (o de un reintento). El normalize NO se pierde.
        remaining = int(report.not_found or 0) + int(report.errors or 0)
        if isinstance(job.report, dict):
            job.report = {**job.report, "needs_geocode": remaining}

        # El nested se genero durante normalize, ANTES de tener coordenadas. Si no
        # se regenera, `download?format=nested` (que es lo que consume la UI)
        # devuelve la version vieja y todo el geocoding queda invisible.
        _refresh_nested(job, output)
        job.geocode_progress = {"phase": "done", "done": report.rows, "total": report.rows}
        job.op_started_at = None
        # Si quedaron sin coords, COMPLETED igual — la UI las pide a mano.
        job.touch(COMPLETED)
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
        logger.exception("geocode fallo en %s (normalize conservado)", job_id)


def _refresh_nested(job: Job, geocoded_csv: Path) -> None:
    """Regenera el JSON anidado a partir del CSV geocodificado.

    El CSV geocodificado ya esta en formato Vepathos, asi que vuelve a pasar por
    el pipeline sin cambios (round-trip) y sale el nested con las coordenadas.
    """
    from ..pipeline import run_normalize

    try:
        destino = store.dir_for(job.id) / "geocoded" / "geocoded.csv"
        resultado = run_normalize(
            geocoded_csv, _schema_path(job.schema), destino,
            emit=("nested",), config=CFG,
            # El CSV ya es Vepathos flat con address enriquecida: re-detectar
            # "varios campos" y extraer destruye filas (32→11). Solo round-trip.
            expand_composite=False,
        )
        if nested := resultado.outputs.get("nested"):
            job.nested_path = nested
            stage(logger, "EMIT", "nested regenerado con las coordenadas nuevas",
                  entregas=len(resultado.deliveries))
    except Exception as exc:
        # que falle el refresco no puede invalidar un geocoding que salio bien
        stage(logger, "WARN", f"no se pudo regenerar el nested tras geocodificar: {exc}",
              level=logging.WARNING)


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



