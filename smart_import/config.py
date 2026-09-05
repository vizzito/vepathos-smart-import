"""Configuracion por environment variables.

Regla: TODO lo configurable vive aca y sale de un env var con default razonable.
El servicio arranca sin ningun env seteado, y el mismo binario sirve para local
y para produccion cambiando unicamente el archivo .env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields


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


def _str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw.strip() if raw and raw.strip() else default


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Config:
    # ---------------- limites de entrada ----------------
    max_file_mb: float = 10.0
    max_rows: int = 50_000

    # ---------------- deteccion de schema ----------------
    auto_accept_threshold: float = 0.90
    review_threshold: float = 0.70
    sample_rows: int = 20
    schema_dir: str = "schemas"
    default_schema: str = "vepathos_flat_v1"

    # ---------------- servicio HTTP ----------------
    host: str = "0.0.0.0"
    port: int = 8100
    cors_origins: tuple[str, ...] = ("*",)
    work_dir: str = "data/jobs"
    verbose: bool = False

    # ---------------- IA (opcional) ----------------
    ai_enabled: bool = False
    ai_preload: bool = False          # cargar el modelo al arrancar, no en el 1er request
    model: str = "numind/NuExtract-1.5-tiny"
    device: str = "cpu"
    ai_timeout_s: float = 30.0
    extract_max_rows: int = 2_000
    extract_workers: int = 1

    # ---------------- geocoding (opcional) ----------------
    geocoding_enabled: bool = True
    pbf_dir: str = ""
    index_dir: str = "data/indexes"
    cache_path: str = "data/cache/geocode_cache.sqlite"
    geocoder_fallback: str = "none"
    match_threshold: float = 0.90
    low_confidence_threshold: float = 0.70
    geocode_workers: int = 1
    autobuild_index: bool = True      # construir el indice si falta

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            max_file_mb=_float("SMART_IMPORT_MAX_FILE_MB", 10.0),
            max_rows=_int("SMART_IMPORT_MAX_ROWS", 50_000),

            auto_accept_threshold=_float("AUTO_ACCEPT_THRESHOLD", 0.90),
            review_threshold=_float("REVIEW_THRESHOLD", 0.70),
            sample_rows=_int("SMART_IMPORT_SAMPLE_ROWS", 20),
            schema_dir=_str("SMART_IMPORT_SCHEMA_DIR", "schemas"),
            default_schema=_str("SMART_IMPORT_DEFAULT_SCHEMA", "vepathos_flat_v1"),

            host=_str("SMART_IMPORT_HOST", "0.0.0.0"),
            port=_int("SMART_IMPORT_PORT", 8100),
            cors_origins=tuple(_list("SMART_IMPORT_CORS_ORIGINS", ["*"])),
            work_dir=_str("SMART_IMPORT_WORK_DIR", "data/jobs"),
            verbose=_bool("SMART_IMPORT_VERBOSE", False),

            ai_enabled=_bool("SMART_IMPORT_AI_ENABLED", False),
            ai_preload=_bool("SMART_IMPORT_AI_PRELOAD", False),
            model=_str("SMART_IMPORT_MODEL", "numind/NuExtract-1.5-tiny"),
            device=_str("SMART_IMPORT_DEVICE", "cpu"),
            ai_timeout_s=_float("SMART_IMPORT_AI_TIMEOUT", 30.0),
            extract_max_rows=_int("SMART_IMPORT_EXTRACT_MAX_ROWS", 2_000),
            extract_workers=_int("SMART_IMPORT_EXTRACT_WORKERS", 1),

            geocoding_enabled=_bool("SMART_IMPORT_GEOCODING_ENABLED", True),
            pbf_dir=_str("SMART_IMPORT_PBF_DIR", ""),
            index_dir=_str("SMART_IMPORT_INDEX_DIR", "data/indexes"),
            cache_path=_str("SMART_IMPORT_CACHE_PATH", "data/cache/geocode_cache.sqlite"),
            geocoder_fallback=_str("GEOCODER_FALLBACK", "none"),
            match_threshold=_float("GEOCODE_MATCH_THRESHOLD", 0.90),
            low_confidence_threshold=_float("GEOCODE_LOW_CONFIDENCE_THRESHOLD", 0.70),
            geocode_workers=_int("SMART_IMPORT_GEOCODE_WORKERS", 1),
            autobuild_index=_bool("SMART_IMPORT_AUTOBUILD_INDEX", True),
        )

    def replace(self, **changes) -> "Config":
        current = {f.name: getattr(self, f.name) for f in fields(self)}
        current.update(changes)
        return Config(**current)

    def describe(self) -> dict:
        """Config efectiva, para /health y para el log de arranque."""
        return {f.name: getattr(self, f.name) for f in fields(self)}
