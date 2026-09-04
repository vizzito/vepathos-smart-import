"""Almacen de jobs: metadata en memoria, archivos en disco.

Deliberadamente simple para el MVP: un proceso, sin base de datos ni cola. La
forma de los estados y del mensaje ya es la que va a usar el consumer de
RabbitMQ, asi que migrar despues es cambiar el almacen, no el pipeline.
"""
from __future__ import annotations

import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# estados del job (los mismos que va a usar la integracion con routehub)
UPLOADED = "uploaded"
ANALYZING = "analyzing"
NEEDS_REVIEW = "needs_mapping_review"
NORMALIZED = "normalized"          # estado FINAL valido aunque nunca se geocodifique
EXTRACTING = "extracting"
GEOCODE_QUEUED = "geocode_queued"
GEOCODING = "geocoding"
COMPLETED = "completed"
FAILED = "failed"

ALLOWED_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls", ".json"}
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str | None) -> str:
    """El nombre lo elige el usuario: nunca se usa crudo para armar una ruta."""
    base = Path(name or "upload").name
    cleaned = _UNSAFE.sub("_", base).strip("._-") or "upload"
    return cleaned[:120]


@dataclass
class Job:
    id: str
    filename: str
    status: str = UPLOADED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    schema: str = "vepathos_flat_v1"
    raw_path: str | None = None
    normalized_path: str | None = None
    nested_path: str | None = None
    geocoded_path: str | None = None
    report: dict = field(default_factory=dict)
    geocode_report: dict = field(default_factory=dict)
    geocode_progress: dict = field(default_factory=dict)
    extract_report: dict = field(default_factory=dict)
    extract_progress: dict = field(default_factory=dict)
    error: str | None = None

    def touch(self, status: str | None = None) -> None:
        if status:
            self.status = status
        self.updated_at = time.time()

    @property
    def composite_column(self) -> str | None:
        """Columna marcada como 'mezcla varios campos', si el mapper detecto una."""
        for column, info in (self.report.get("mapping") or {}).items():
            if "varios campos" in (info.get("evidence") or ""):
                return column
        return None

    @property
    def needs_geocode(self) -> int:
        return int(self.report.get("needs_geocode", 0))

    def next_actions(self) -> list[dict[str, str]]:
        """Que puede hacer el usuario ahora. Geocodificar SIEMPRE es opt-in."""
        actions: list[dict[str, str]] = []
        if self.status in (NORMALIZED, NEEDS_REVIEW, COMPLETED):
            actions.append({
                "action": "download",
                "href": f"/imports/{self.id}/download?format=flat",
                "description": "Descargar el archivo normalizado (formato Vepathos)",
            })
        if self.status in (NORMALIZED, NEEDS_REVIEW) and self.composite_column:
            actions.append({
                "action": "extract",
                "href": f"/imports/{self.id}/extract",
                "description": (f"Separar '{self.composite_column}' en nombre/direccion/"
                                "telefono con el modelo. Opt-in, ~1.4 s por fila."),
            })
        if self.status == NEEDS_REVIEW:
            actions.append({
                "action": "confirm_mapping",
                "href": f"/imports/{self.id}/mapping",
                "description": "Corregir el mapping de las columnas dudosas y re-normalizar",
            })
        if self.status in (NORMALIZED, NEEDS_REVIEW) and self.needs_geocode:
            actions.append({
                "action": "geocode",
                "href": f"/imports/{self.id}/geocode",
                "description": (f"Geolocalizar {self.needs_geocode} fila(s) sin coordenadas. "
                                "NO se ejecuta solo: lo decide el usuario."),
            })
        return actions

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "status": self.status,
            "filename": self.filename,
            "schema": self.schema,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "report": self.report,
            "geocode": {**self.geocode_report, "progress": self.geocode_progress}
                       if (self.geocode_report or self.geocode_progress) else None,
            "extract": {**self.extract_report, "progress": self.extract_progress}
                       if (self.extract_report or self.extract_progress) else None,
            "error": self.error,
            "next_actions": self.next_actions(),
        }


class JobStore:
    def __init__(self, work_dir: str | Path):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, filename: str, schema: str) -> Job:
        job_id = f"imp_{uuid.uuid4().hex[:12]}"
        job = Job(id=job_id, filename=safe_filename(filename), schema=schema)
        with self._lock:
            self._jobs[job_id] = job
        (self.dir_for(job_id) / "raw").mkdir(parents=True, exist_ok=True)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, limit: int = 50) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: -j.created_at)[:limit]

    def dir_for(self, job_id: str) -> Path:
        return self.work_dir / job_id

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        shutil.rmtree(self.dir_for(job_id), ignore_errors=True)
        return True
