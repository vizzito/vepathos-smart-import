"""Artefactos en el disco de este proceso. Es lo que hace el servicio hoy.

Cada job es una carpeta bajo `work_dir`, con el mismo layout de siempre
(`raw/`, `normalized/`, `geocoded/`): un despliegue existente sigue encontrando
sus archivos donde estaban, y `JobStore.delete` sigue barriendo la misma carpeta.

`publish` no copia nada porque la ruta que devolvio `reserve` YA es la
definitiva. Ese no-op es a proposito: es lo que hace que esta fase no cambie ni
un byte del comportamiento.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .base import ArtifactStore, relative_path


class LocalArtifactStore(ArtifactStore):
    def __init__(self, work_dir: str | Path):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def dir_for(self, job_id: str) -> Path:
        return self.work_dir / job_id

    def reserve(self, job_id: str, kind: str, *, filename: str | None = None) -> Path:
        path = self.dir_for(job_id) / relative_path(kind, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def publish(self, job_id: str, kind: str, path: str | Path) -> str:
        return str(path)

    def resolve(self, job_id: str, kind: str, ref: str | Path | None) -> Path | None:
        # La referencia del `Job` manda: hay artefactos que no estan en su lugar
        # canonico (el nested regenerado vive en `geocoded/`) y hay tests que
        # apuntan un job a un archivo suelto. El layout es solo el fallback.
        candidato = Path(ref) if ref else self.dir_for(job_id) / relative_path(kind)
        return candidato if candidato.exists() else None

    def delete(self, job_id: str) -> None:
        shutil.rmtree(self.dir_for(job_id), ignore_errors=True)
