"""Donde viven los archivos de un job.

La factory elige el backend por configuracion. Hoy hay uno solo (`local`, el
comportamiento de siempre); la fase que reparte el trabajo entre nodos agrega el
que intercambia bytes por HTTP con el rol api, sin tocar a los que llaman.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import (
    FLAT, GEOCODED, GEOCODED_NESTED, GEOCODED_REPORT, KINDS, LAYOUT, NESTED,
    RAW, REPORT, ArtifactStore, UnknownKind, relative_path,
)
from .local import LocalArtifactStore

if TYPE_CHECKING:
    from ..config import Config

__all__ = [
    "ArtifactStore", "LocalArtifactStore", "UnknownKind", "make_artifact_store",
    "relative_path", "KINDS", "LAYOUT",
    "RAW", "FLAT", "NESTED", "REPORT", "GEOCODED", "GEOCODED_NESTED",
    "GEOCODED_REPORT",
]


def make_artifact_store(cfg: "Config") -> ArtifactStore:
    return LocalArtifactStore(cfg.work_dir)
