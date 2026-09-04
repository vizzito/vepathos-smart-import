"""API HTTP de Smart Import.

Diseno:
  - `normalize` es SINCRONO: 50k filas tardan ~1.5 s, no justifica una cola.
  - `geocode` es ASINCRONO y SIEMPRE explicito: nunca se dispara solo.
  - el archivo subido no viaja mas alla de esta capa; se guarda en disco y el
    resto del pipeline trabaja con rutas (misma forma que tendra con RabbitMQ).
"""
from __future__ import annotations

import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from ..config import Config
from ..schemas import TargetSchema
from .jobs import (
    ALLOWED_SUFFIXES, COMPLETED, EXTRACTING, FAILED, GEOCODE_QUEUED, GEOCODING,
    NEEDS_REVIEW, NORMALIZED, Job, JobStore, safe_filename,
)

logger = logging.getLogger("smart_import.api")

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "schemas"
WORK_DIR = Path(Config.from_env().__dict__.get("work_dir", "data/jobs"))

# Geocoding en un solo worker a proposito: comparte la VM con el cutter y no
# tiene que competirle CPU. Se sube cuando haya medicion que lo justifique.
_geocode_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="geocode")
# La extraccion con modelo cuesta ~1.4 s por fila: un solo worker, igual que geocode.
_extract_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="extract")

app = FastAPI(
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
    allow_origins=["*"],          # ajustar al dominio del cliente en produccion
    allow_methods=["*"],
    allow_headers=["*"],
)

store = JobStore(WORK_DIR)


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

    cfg = Config.from_env()
    registry = PbfRegistry.scan(cfg.pbf_dir) if cfg.pbf_dir else PbfRegistry([])
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
        "ai": {
            "enabled": cfg.ai_enabled,
            "dependencies_installed": ai_ready,
            "model": cfg.model,
            "device": cfg.device,
            # sin IA el sistema sigue funcionando entero con reglas
            "degrades_to_rules": True,
        },
        "geocoding": {
            "pbf_dir": cfg.pbf_dir or None,
            "pbf_available": len(registry.entries),
            "indexes_built": indexes,
            "fallback": cfg.geocoder_fallback,
            "automatic": False,        # NUNCA se dispara solo
        },
        "jobs": len(store.list(limit=10_000)),
    }


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
    return _run_normalize(job, schema_path, phone_region, diagnostics)


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
    return _job_or_404(job_id).as_dict()


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

    job.geocode_progress = {"done": 0, "total": job.report.get("rows_output", 0)}
    job.geocode_report = {}
    job.touch(GEOCODE_QUEUED)
    _geocode_pool.submit(_geocode_worker, job.id, origin, box, index)
    return {**job.as_dict(), "poll": f"/imports/{job_id}"}


def _geocode_worker(job_id: str, origin, box, index_name: str | None) -> None:
    from ..geocoding.osm_index import build, index_path_for
    from ..geocoding.pbf_registry import PbfRegistry
    from ..geocoding.runner import run

    job = store.get(job_id)
    if job is None:
        return
    cfg = Config.from_env()
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
                # el PBF se procesa UNA vez por region y queda cacheado
                job.geocode_progress = {"phase": "building_index", "pbf": entry.path.name}
                build(entry.path, index_path)

        def progress(done: int, report) -> None:
            job.geocode_progress = {
                "phase": "geocoding", "done": done,
                "total": job.report.get("rows_output", 0),
                "matched": report.matched, "low_confidence": report.low_confidence,
                "not_found": report.not_found,
            }

        out_dir = store.dir_for(job_id) / "geocoded"
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / "geocoded.csv"

        report = run(job.normalized_path, output, index_path, origin=origin, bbox=box,
                     config=cfg, progress=progress)
        job.geocoded_path = str(output)
        job.geocode_report = report.as_dict()
        job.geocode_progress = {"phase": "done", "done": report.rows, "total": report.rows}
        job.touch(COMPLETED)
    except Exception as exc:
        job.error = str(exc)
        job.geocode_progress = {"phase": "failed"}
        job.touch(FAILED)
        logger.exception("geocode fallo en %s", job_id)


@app.get("/geocoding/coverage", tags=["geocode"])
def coverage(lat: float = Query(...), lon: float = Query(...)) -> dict[str, Any]:
    """Hay cobertura OSM para este punto? Sirve para decidir ANTES de ofrecer el boton."""
    from ..geocoding.osm_index import index_path_for
    from ..geocoding.pbf_registry import PbfRegistry

    cfg = Config.from_env()
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
    job = _job_or_404(job_id)
    if not job.normalized_path or not Path(job.normalized_path).exists():
        raise HTTPException(409, "hay que normalizar el archivo antes de extraer")
    if job.status == EXTRACTING:
        raise HTTPException(409, "ya hay una extraccion en curso para este job")

    names = tuple(f.strip() for f in fields.split(",") if f.strip())
    if not names:
        raise HTTPException(400, "indica al menos un campo en 'fields'")

    job.extract_progress = {"phase": "queued", "done": 0}
    job.touch(EXTRACTING)
    _extract_pool.submit(_extract_worker, job.id, column, names, max_rows)
    return {**job.as_dict(), "poll": f"/imports/{job_id}"}


def _extract_worker(job_id: str, column: str, fields: tuple[str, ...],
                    max_rows: int | None) -> None:
    import csv

    from ..extraction import CompositeExtractor

    job = store.get(job_id)
    if job is None:
        return
    cfg = Config.from_env()
    try:
        with open(job.normalized_path, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        if not rows or column not in rows[0]:
            raise ValueError(f"la columna '{column}' no existe en el resultado normalizado")

        def progress(done: int, res) -> None:
            job.extract_progress = {"phase": "extracting", "done": done,
                                    "extracted": res.extracted, "failed": res.failed}

        extractor = CompositeExtractor(cfg, fields=fields)
        result = extractor.run([r.get(column) or "" for r in rows],
                               max_rows=max_rows or cfg.extract_max_rows, progress=progress)

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
        job.extract_progress = {"phase": "done", "done": result.rows}
        job.touch(NORMALIZED)
    except Exception as exc:
        job.error = str(exc)
        job.extract_progress = {"phase": "failed"}
        job.touch(FAILED)
        logger.exception("extract fallo en %s", job_id)
