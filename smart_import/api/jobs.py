"""Compat: el almacen de jobs se mudo a `smart_import.jobs`.

Se movio para que el worker pueda importar `Job` y `JobStore` sin arrastrar el
paquete `api`, cuyo `__init__` importa `app.py` y con el fastapi, el `Config` y
un `JobStore` en tiempo de import. Este modulo queda como alias del anterior.
"""
from __future__ import annotations

from ..jobs import (  # noqa: F401
    ALLOWED_SUFFIXES, ANALYZING, BUSY_STATUSES, COMPLETED, FAILED,
    GEOCODE_FAILED, GEOCODE_QUEUED, GEOCODING, HAS_NORMALIZE, NEEDS_REVIEW,
    NORMALIZED, UPLOADED, Job, JobStore, safe_filename,
)

__all__ = [
    "ALLOWED_SUFFIXES", "ANALYZING", "BUSY_STATUSES", "COMPLETED", "FAILED",
    "GEOCODE_FAILED", "GEOCODE_QUEUED", "GEOCODING", "HAS_NORMALIZE",
    "NEEDS_REVIEW", "NORMALIZED", "UPLOADED", "Job", "JobStore", "safe_filename",
]
