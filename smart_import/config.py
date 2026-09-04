"""Configuracion por environment variables.

Todo tiene default razonable: el CLI corre sin ningun env seteado.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # --- limites de entrada ---
    max_file_mb: float = 10.0
    max_rows: int = 50_000

    # --- deteccion de schema ---
    auto_accept_threshold: float = 0.90
    review_threshold: float = 0.70
    sample_rows: int = 20

    # --- IA (opcional, apagada por defecto) ---
    ai_enabled: bool = False
    model: str = "numind/NuExtract-1.5-tiny"
    device: str = "cpu"
    ai_timeout_s: float = 30.0

    # --- geocoding ---
    pbf_dir: str = ""
    index_dir: str = "data/indexes"
    cache_path: str = "data/cache/geocode_cache.sqlite"
    geocoder_fallback: str = "none"
    match_threshold: float = 0.90
    low_confidence_threshold: float = 0.70

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            max_file_mb=_float("SMART_IMPORT_MAX_FILE_MB", 10.0),
            max_rows=_int("SMART_IMPORT_MAX_ROWS", 50_000),
            auto_accept_threshold=_float("AUTO_ACCEPT_THRESHOLD", 0.90),
            review_threshold=_float("REVIEW_THRESHOLD", 0.70),
            sample_rows=_int("SMART_IMPORT_SAMPLE_ROWS", 20),
            ai_enabled=_bool("SMART_IMPORT_AI_ENABLED", False),
            model=os.getenv("SMART_IMPORT_MODEL", "numind/NuExtract-1.5-tiny"),
            device=os.getenv("SMART_IMPORT_DEVICE", "cpu"),
            ai_timeout_s=_float("SMART_IMPORT_AI_TIMEOUT", 30.0),
            pbf_dir=os.getenv("SMART_IMPORT_PBF_DIR", ""),
            index_dir=os.getenv("SMART_IMPORT_INDEX_DIR", "data/indexes"),
            cache_path=os.getenv("SMART_IMPORT_CACHE_PATH", "data/cache/geocode_cache.sqlite"),
            geocoder_fallback=os.getenv("GEOCODER_FALLBACK", "none"),
            match_threshold=_float("GEOCODE_MATCH_THRESHOLD", 0.90),
            low_confidence_threshold=_float("GEOCODE_LOW_CONFIDENCE_THRESHOLD", 0.70),
        )
