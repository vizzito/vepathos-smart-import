"""Donde viven los archivos de un job.

La factory elige el backend por configuracion. Hoy hay uno solo (`local`, el
comportamiento de siempre); la fase que reparte el trabajo entre nodos agrega el
que intercambia bytes por HTTP con el rol api, sin tocar a los que llaman.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import (
    FLAT, GEOCODED, GEOCODED_NESTED, GEOCODED_REPORT, KINDS, LAYOUT, NESTED,
    PARCIAL, RAW, REPORT, URI_SCHEME, ArtifactStore, UnknownKind, artifact_uri,
    local_path, relative_path,
)
from .http import (
    ArtifactRejected, ArtifactTransferError, HttpArtifactStore, default_scratch,
)
from .local import LocalArtifactStore

if TYPE_CHECKING:
    from ..config import Config

__all__ = [
    "ArtifactStore", "LocalArtifactStore", "HttpArtifactStore",
    "ArtifactTransferError", "ArtifactRejected", "UnknownKind",
    "make_artifact_store", "default_scratch",
    "relative_path", "artifact_uri", "local_path", "KINDS", "LAYOUT",
    "URI_SCHEME", "PARCIAL",
    "RAW", "FLAT", "NESTED", "REPORT", "GEOCODED", "GEOCODED_NESTED",
    "GEOCODED_REPORT",
]


def make_artifact_store(cfg: "Config") -> ArtifactStore:
    """El backend que le toca a este proceso segun su rol.

    Un worker no tiene los archivos del usuario: los pide a la api. Que no haya
    a quien pedirselos es un error de despliegue y se dice al arrancar, no
    cuando la primera tarea falle a mitad de camino.
    """
    if cfg.role != "worker":
        return LocalArtifactStore(cfg.work_dir)

    faltan = [nombre for nombre, valor in
              (("SMART_IMPORT_API_URL", cfg.api_url),
               ("SMART_IMPORT_WORKER_TOKEN", cfg.worker_token)) if not valor]
    if faltan:
        raise RuntimeError(
            f"el rol worker busca los archivos en la api y le falta: "
            f"{', '.join(faltan)}")
    return HttpArtifactStore(cfg.api_url, cfg.worker_token,
                             cfg.scratch_dir or default_scratch())
