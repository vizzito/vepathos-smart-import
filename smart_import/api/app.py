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
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from ..config import Config
from ..schemas import TargetSchema
from .jobs import (
    ALLOWED_SUFFIXES, COMPLETED, EXTRACTING, FAILED,
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
_extract_pool = ThreadPoolExecutor(max_workers=max(1, CFG.extract_workers),
                                   thread_name_prefix="extract")


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
          ia="on" if CFG.ai_enabled else "off")

    if CFG.geocoding_enabled and not CFG.pbf_dir:
        stage(logger, "WARN", "geocoding habilitado pero SMART_IMPORT_PBF_DIR esta vacio: "
                              "no va a poder resolver ninguna direccion",
              level=logging.WARNING)

    if CFG.ai_enabled and CFG.ai_preload:
        # Cargar aca y no en el primer request: la descarga del modelo son ~60 s
        # la primera vez, y no queremos que la pague un usuario.
        try:
            from ..models.loader import load
            stage(logger, "EXTRACT", "precargando modelo", modelo=CFG.model, device=CFG.device)
            loaded = load(CFG.model, CFG.device)
            stage(logger, "EXTRACT", "modelo listo", carga=f"{loaded.load_seconds:.1f}s")
        except Exception as exc:
            # que el modelo no cargue NO puede impedir que el servicio arranque
            stage(logger, "WARN", f"no se pudo precargar el modelo ({exc}); "
                                  "el resto del servicio funciona igual",
                  level=logging.WARNING)
    yield
    stage(logger, "HTTP", "cerrando")

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
    from ..geocoding.pbf_registry import PbfRegistry
    from ..models.loader import is_loaded

    cfg = CFG
    registry = (PbfRegistry.scan(cfg.pbf_dir)
                if cfg.geocoding_enabled and cfg.pbf_dir else PbfRegistry([]))
    indexes = sorted(p.name for p in Path(cfg.index_dir).glob("*.sqlite")) \
        if Path(cfg.index_dir).exists() else []

    # se chequea SIEMPRE, no solo cuando esta habilitada: saber si las
    # dependencias estan es distinto de saber si el flag esta prendido
    try:
        import torch, transformers              # noqa: F401
        ai_ready = True
    except ImportError:
        ai_ready = False

    return {
        "status": "ok",
        "version": app.version,
        "limits": {"max_file_mb": cfg.max_file_mb, "max_rows": cfg.max_rows},
        "schemas": sorted(p.stem for p in SCHEMA_DIR.glob("*.json")),
        "capabilities": {
            "normalize": True,                        # siempre; es el nucleo
            "geocoding": cfg.geocoding_enabled,
            "extract": cfg.ai_enabled and ai_ready,
        },
        "ai": {
            "enabled": cfg.ai_enabled,
            "dependencies_installed": ai_ready,
            "preloaded": is_loaded(cfg.model, cfg.device) if ai_ready else False,
            "model": cfg.model,
            "device": cfg.device,
            "max_rows": cfg.extract_max_rows,
            # sin IA el sistema sigue funcionando entero con reglas
            "degrades_to_rules": True,
        },
        "geocoding": {
            "enabled": cfg.geocoding_enabled,
            "pbf_dir": cfg.pbf_dir or None,
            "pbf_available": len(registry.entries),
            "indexes_built": indexes,
            "autobuild_index": cfg.autobuild_index,
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
                        "extract": CFG.ai_enabled}
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
    response = _run_normalize(job, schema_path, phone_region, diagnostics)
    stage(logger, "HTTP", "201 creado", job=job.id, estado=job.status,
          acciones=",".join(a["action"] for a in job.next_actions()))
    return response


def _run_normalize(job: Job, schema_path: Path, phone_region: str | None,
                   diagnostics: bool, manual_mapping: dict | None = None) -> dict[str, Any]:
    from ..pipeline import run_normalize

    out_dir = store.dir_for(job.id) / "normalized"
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / "normalized.csv"

    job.touch(NORMALIZED)
    try:
        result = run_normalize(
            job.raw_path, schema_path, output, emit=("flat", "nested"),
            manual_mapping=manual_mapping, phone_region=phone_region,
            diagnostics=diagnostics,
        )
    except Exception as exc:
        job.error = str(exc)
        job.touch(FAILED)
        logger.exception("normalize fallo en %s", job.id)
        raise HTTPException(422, f"no se pudo procesar el archivo: {exc}") from exc

    job.report = result.report
    job.normalized_path = result.outputs.get("flat")
    job.nested_path = result.outputs.get("nested")
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
            source: str = Query("normalized", pattern="^(normalized|geocoded)$")) -> dict[str, Any]:
    """Muestra de las filas resultantes, para que la UI las pinte antes de importar."""
    import csv

    job = _job_or_404(job_id)
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
def issues(job_id: str, limit: int = Query(200, le=1000)) -> dict[str, Any]:
    """Las filas que NO quedaron listas, con el motivo y la columna culpable.

    Ninguna fila se descarta: todas estan en el archivo de salida. Este endpoint
    existe para que la UI pueda mostrar "3 listas, 3 a geocodificar, 1 con
    problema" en vez de perder filas en silencio.
    """
    job = _job_or_404(job_id)
    report = job.report or {}
    if not report:
        raise HTTPException(409, "el job todavia no fue normalizado")

    filas = report.get("row_issues", [])[:limit]
    geo = job.geocode_report or {}

    return {
        "job_id": job_id,
        "summary": {
            "total": report.get("rows_output", 0),
            "listas": report.get("valid_rows", 0),
            "a_geocodificar": report.get("needs_geocode", 0),
            "no_localizables": report.get("invalid_rows", 0),
            "con_observaciones": report.get("rows_with_issues", 0),
            "coordenadas_rechazadas": report.get("rejected_coordinates", 0),
            **({"geocodificadas": geo.get("matched", 0),
                "geocode_confianza_baja": geo.get("low_confidence", 0),
                "geocode_no_encontradas": geo.get("not_found", 0)} if geo else {}),
        },
        # columnas del archivo que el mapper no supo ubicar: no se pierden datos,
        # simplemente no entran al schema
        "columnas_sin_mapear": report.get("unmapped", []),
        "columnas_a_revisar": report.get("ambiguous", []),
        "avisos": report.get("warnings", []),
        "filas": filas,
        "truncado": len(report.get("row_issues", [])) > limit,
    }


@app.put("/imports/{job_id}/mapping", tags=["import"])
def confirm_mapping(
    job_id: str,
    mapping: dict[str, str | None] = Body(..., examples=[{"Dest.": "address", "Obs": None}]),
    phone_region: str | None = Query(None),
    diagnostics: bool = Query(False),
) -> dict[str, Any]:
    """Corrige el mapping sugerido y vuelve a normalizar.

    Lo que decide el usuario es final: se marca `manual` y confianza 1.0.
    """
    job = _job_or_404(job_id)
    if not job.raw_path or not Path(job.raw_path).exists():
        raise HTTPException(409, "el archivo original ya no esta disponible")
    return _run_normalize(job, _schema_path(job.schema), phone_region, diagnostics,
                          manual_mapping=mapping)


@app.get("/imports/{job_id}/download", tags=["import"])
def download(job_id: str,
             format: str = Query("flat", pattern="^(flat|nested|geocoded)$")) -> FileResponse:
    """Descarga el resultado. `flat` = CSV Vepathos, `nested` = JSON del optimizador."""
    job = _job_or_404(job_id)
    path = {"flat": job.normalized_path, "nested": job.nested_path,
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
) -> dict[str, Any]:
    """Arranca la geolocalizacion de las filas que NO tienen coordenadas.

    Es una accion explicita: el pipeline nunca la dispara por su cuenta. Las
    filas que ya traian coordenadas se conservan intactas.
    """
    _capability_guard(CFG.geocoding_enabled, "El geocoding",
                      "Habilitalo con SMART_IMPORT_GEOCODING_ENABLED=true.")
    _capability_guard(bool(CFG.pbf_dir), "El geocoding",
                      "Falta SMART_IMPORT_PBF_DIR apuntando a los .osm.pbf.")
    job = _job_or_404(job_id)
    if job.status in (GEOCODING, GEOCODE_QUEUED):
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

    total = int(job.report.get("rows_output") or job.needs_geocode or 0)
    stage(logger, "HTTP", "POST /imports/{id}/geocode  (accion EXPLICITA del usuario)",
          job=job_id, depot=f"{origin_lat},{origin_lon}" if origin else None,
          filas_a_geocodificar=job.needs_geocode)
    job.op_started_at = time.time()
    job.geocode_progress = {"phase": "queued", "done": 0, "total": total}
    job.geocode_report = {}
    job.touch(GEOCODE_QUEUED)
    _geocode_pool.submit(_geocode_worker, job.id, origin, box, index)
    return {**job.as_dict(),
            "poll": f"/imports/{job_id}",
            "events": f"/imports/{job_id}/events"}


def _geocode_worker(job_id: str, origin, box, index_name: str | None) -> None:
    from ..geocoding.osm_index import build, index_path_for
    from ..geocoding.pbf_registry import PbfRegistry
    from ..geocoding.runner import run

    job = store.get(job_id)
    if job is None:
        return
    cfg = CFG
    job.op_started_at = time.time()
    job.touch(GEOCODING)

    try:
        if index_name:
            index_path = Path(cfg.index_dir) / index_name
            if not index_path.exists():
                raise FileNotFoundError(f"no existe el indice {index_path}")
        else:
            registry = PbfRegistry.scan(cfg.pbf_dir)
            lat = origin[0] if origin else None
            lon = origin[1] if origin else None
            entry = registry.resolve(lat=lat, lon=lon, bbox=box)
            if entry is None:
                raise FileNotFoundError(
                    f"sin cobertura PBF para el area pedida en {cfg.pbf_dir}. "
                    f"({len(registry.entries)} PBF disponibles)")
            index_path = index_path_for(entry, cfg.index_dir)
            if not index_path.exists():
                if not cfg.autobuild_index:
                    raise FileNotFoundError(
                        f"falta el indice {index_path.name} y SMART_IMPORT_AUTOBUILD_INDEX "
                        "esta en false. Generalo con 'build-geocoder-index'.")
                # el PBF se procesa UNA vez por region y queda cacheado
                job.geocode_progress = {
                    "phase": "building_index", "pbf": entry.path.name,
                    "done": 0, "total": int(job.report.get("rows_output") or 0),
                }
                job.touch()
                build(entry.path, index_path)

        total = int(job.report.get("rows_output") or 0)

        def progress(done: int, report) -> None:
            job.geocode_progress = {
                "phase": "geocoding", "done": done, "total": total,
                "matched": report.matched, "low_confidence": report.low_confidence,
                "not_found": report.not_found,
            }
            job.touch()

        out_dir = store.dir_for(job_id) / "geocoded"
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / "geocoded.csv"

        report = run(job.normalized_path, output, index_path, origin=origin, bbox=box,
                     config=cfg, progress=progress)
        job.geocoded_path = str(output)
        job.geocode_report = report.as_dict()
        job.geocode_progress = {"phase": "done", "done": report.rows, "total": report.rows}
        job.op_started_at = None
        job.touch(COMPLETED)
    except Exception as exc:
        job.error = str(exc)
        job.geocode_progress = {"phase": "failed",
                                "done": (job.geocode_progress or {}).get("done", 0),
                                "total": (job.geocode_progress or {}).get("total", 0)}
        job.op_started_at = None
        job.touch(FAILED)
        logger.exception("geocode fallo en %s", job_id)


@app.get("/geocoding/coverage", tags=["geocode"])
def coverage(lat: float = Query(...), lon: float = Query(...)) -> dict[str, Any]:
    """Hay cobertura OSM para este punto? Sirve para decidir ANTES de ofrecer el boton."""
    from ..geocoding.osm_index import index_path_for
    from ..geocoding.pbf_registry import PbfRegistry

    cfg = CFG
    if not cfg.geocoding_enabled:
        return {"available": False, "point": [lat, lon],
                "reason": "el geocoding esta deshabilitado en este despliegue"}
    registry = PbfRegistry.scan(cfg.pbf_dir) if cfg.pbf_dir else PbfRegistry([])
    entry = registry.resolve(lat=lat, lon=lon)
    if entry is None:
        return {"available": False, "point": [lat, lon],
                "reason": "ningun .osm.pbf cubre ese punto",
                "pbf_scanned": len(registry.entries)}
    index_path = index_path_for(entry, cfg.index_dir)
    return {
        "available": True, "point": [lat, lon],
        "pbf": entry.as_dict(),
        "index_ready": index_path.exists(),
        "index": str(index_path),
        "note": ("el indice ya esta construido" if index_path.exists()
                 else "la primera geolocalizacion en esta zona construye el indice "
                      "(una sola vez, ~10 s por cada 25 MB de PBF)"),
    }


# --------------------------------------------------------------- extract (IA)

@app.post("/imports/{job_id}/extract", tags=["extract"], status_code=202)
def start_extract(
    job_id: str,
    column: str = Query("address", description="Columna que mezcla varios campos"),
    fields: str = Query("customer_name,address,phone"),
    max_rows: int | None = Query(None, description="Tope de filas"),
) -> dict[str, Any]:
    """Separa una columna compuesta usando el modelo. UNICA operacion con IA.

    Es opt-in y cuesta ~1.4 s por fila en CPU. Si el modelo no esta instalado,
    el job no se rompe: termina con un aviso y el archivo normalizado sigue
    siendo valido.
    """
    _capability_guard(CFG.ai_enabled, "La extraccion con modelo",
                      "Habilitala con SMART_IMPORT_AI_ENABLED=true "
                      "(requiere la imagen con torch).")
    job = _job_or_404(job_id)
    if not job.normalized_path or not Path(job.normalized_path).exists():
        raise HTTPException(409, "hay que normalizar el archivo antes de extraer")
    if job.status == EXTRACTING:
        raise HTTPException(409, "ya hay una extraccion en curso para este job")

    names = tuple(f.strip() for f in fields.split(",") if f.strip())
    if not names:
        raise HTTPException(400, "indica al menos un campo en 'fields'")

    stage(logger, "HTTP", "POST /imports/{id}/extract  (usa el modelo)",
          job=job_id, columna=column, campos=fields)
    cap = max_rows or CFG.extract_max_rows
    job.op_started_at = time.time()
    job.extract_progress = {"phase": "queued", "done": 0, "total": cap}
    job.touch(EXTRACTING)
    _extract_pool.submit(_extract_worker, job.id, column, names, max_rows)
    return {**job.as_dict(),
            "poll": f"/imports/{job_id}",
            "events": f"/imports/{job_id}/events"}


def _extract_worker(job_id: str, column: str, fields: tuple[str, ...],
                    max_rows: int | None) -> None:
    import csv

    from ..extraction import CompositeExtractor

    job = store.get(job_id)
    if job is None:
        return
    cfg = CFG
    job.op_started_at = time.time()
    try:
        with open(job.normalized_path, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        if not rows or column not in rows[0]:
            raise ValueError(f"la columna '{column}' no existe en el resultado normalizado")

        limit = max_rows or cfg.extract_max_rows
        total = min(len(rows), limit)
        job.extract_progress = {"phase": "loading_model", "done": 0, "total": total}
        job.touch()

        def progress(done: int, res) -> None:
            job.extract_progress = {
                "phase": "extracting", "done": done, "total": total,
                "extracted": res.extracted, "failed": res.failed,
            }
            job.touch()

        extractor = CompositeExtractor(cfg, fields=fields)
        result = extractor.run([r.get(column) or "" for r in rows],
                               max_rows=limit, progress=progress)

        columns = list(rows[0])
        for name in fields:
            if name not in columns:
                columns.append(name)
        for row, values in zip(rows, result.values):
            for name in fields:
                if values.get(name):
                    row[name] = values[name]

        output = store.dir_for(job_id) / "normalized" / "extracted.csv"
        with open(output, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in columns})

        job.normalized_path = str(output)      # las etapas siguientes usan el separado
        job.extract_report = result.as_dict()
        job.extract_progress = {"phase": "done", "done": result.rows, "total": result.rows,
                                "extracted": result.extracted, "failed": result.failed}
        job.op_started_at = None
        job.touch(NORMALIZED)
    except Exception as exc:
        job.error = str(exc)
        job.extract_progress = {"phase": "failed",
                                "done": (job.extract_progress or {}).get("done", 0),
                                "total": (job.extract_progress or {}).get("total", 0)}
        job.op_started_at = None
        job.touch(FAILED)
        logger.exception("extract fallo en %s", job_id)
