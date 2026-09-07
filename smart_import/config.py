"""Configuracion por environment variables.

Regla: TODO lo configurable vive aca y sale de un env var con default razonable.
El servicio arranca sin ningun env seteado, y el mismo binario sirve para local
y para produccion cambiando unicamente el archivo .env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path


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


def parse_csv_list(raw: str | None, default: list[str] | None = None) -> list[str]:
    """Lista CSV (CORS, etc.). Vacio / None → default. Items strippeados."""
    if raw is None or not str(raw).strip():
        return list(default or [])
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _list(name: str, default: list[str]) -> list[str]:
    return parse_csv_list(os.getenv(name), default)


def _band(primary: str, alias_pct: str, default: float) -> float:
    """Lee banda 0–1. Acepta GEOCODE_*_BAND=0.85 o GEOCODE_*_MIN_PCT=85."""
    for name in (primary, alias_pct):
        raw = os.getenv(name)
        if raw is None or not str(raw).strip():
            continue
        try:
            n = float(str(raw).strip())
        except ValueError:
            continue
        if n > 1.0 and n <= 100.0:
            return n / 100.0
        if 0.0 <= n <= 1.0:
            return n
    return default


def _sibling_route_optimizer_data() -> str:
    """data/ del route-optimizer junto a este repo (layout local de workspace)."""
    sibling = Path(__file__).resolve().parents[2] / "route-optimizer-app" / "data"
    return str(sibling) if sibling.is_dir() else ""


def _resolve_pbf_dir() -> str:
    """PBFs del route-optimizer.

    Prioridad de config:
      1. SMART_IMPORT_PBF_DIR  (explicito)
      2. ROUTE_OPTIMIZER_DATA  (raiz data/: *_tile_* por continente + _extracts)
      3. ../route-optimizer-app/data si existe (dev local)
      4. vacio
    """
    explicit = _str("SMART_IMPORT_PBF_DIR", "")
    if explicit:
        return explicit
    root = _str("ROUTE_OPTIMIZER_DATA", "")
    if root:
        return root
    return _sibling_route_optimizer_data()


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

    # ---------------- extraccion determinística ----------------
    #: region ISO por defecto para telefonos; el job puede sobrescribirla
    default_phone_region: str = ""
    #: un segmento es una entrega a partir de este score
    delivery_accept_threshold: float = 0.55
    #: entre review y accept: probable entrega, pero que la mire un humano
    delivery_review_threshold: float = 0.30
    #: por debajo de esto una direccion NO se acepta ni se manda al geocoder
    address_accept_threshold: float = 0.50
    name_accept_threshold: float = 0.50
    phone_accept_threshold: float = 0.60
    #: TXT: cuanta evidencia estructural hace falta para tratarlo como tabla
    text_tabular_min_consistency: float = 0.80
    text_tabular_min_lines: int = 3
    text_max_prose_ratio: float = 0.20
    text_min_header_score: float = 0.50

    # ---------------- parsing de direcciones ----------------
    #: Solo para benchmarks: heuristic | libpostal | hybrid | enhanced.
    #: En produccion manda `libpostal_enabled`: el heuristico siempre corre y
    #: libpostal es un paso de calidad opcional (ver addresses/enhance.py).
    address_parser: str = "heuristic"
    #: Master switch. false = libpostal no se considera (ni import, ni enhance).
    libpostal_enabled: bool = False

    # ---------------- geocoding (opcional) ----------------
    geocoding_enabled: bool = True
    pbf_dir: str = ""
    index_dir: str = "data/indexes"
    #: Extracts propios (escribible). No es `_extracts` del cutter (:ro).
    extract_dir: str = "data/extracts"
    cache_path: str = "data/cache/geocode_cache.sqlite"
    geocoder_fallback: str = "none"
    match_threshold: float = 0.81
    # Acepta coords desde este score (UI: Review 70–80%, Valid ≥81%).
    low_confidence_threshold: float = 0.70
    # ---- bandas que consume la UI (una sola fuente de verdad) ----
    #: >= esto: verde, la coordenada se usa tal cual (GEOCODE_VALID_BAND / _MIN_PCT)
    geocode_valid_band: float = 0.85
    #: >= esto y < valid: ambar Review (GEOCODE_REVIEW_BAND / _MIN_PCT)
    geocode_review_band: float = 0.75
    #: score crudo minimo para dar pin a un match A NIVEL CALLE (sin altura)
    geocode_street_level_floor: float = 0.60
    #: cuan fuerte tiene que matchear la CALLE para aceptar cualquier coordenada.
    #: Sin esto, coincidir solo en la altura manda el pin a otra calle.
    geocode_street_match_min: float = 0.80
    #: Si True, candidatos que antes eran not_found (calle mala / debajo umbral)
    #: salen como Review CON pin para poder medir distancia en el mapa.
    geocode_soft_reject: bool = True
    #: score minimo para soft-reject (debajo = not_found duro sin pin)
    geocode_soft_reject_min: float = 0.50
    # low_confidence mas lejos que esto del depot se trata como not_found
    max_low_confidence_km: float = 15.0
    # hard geofence: cualquier match (matched|low) fuera de este radio = not_found
    max_geocode_distance_km: float = 500.0
    geocode_workers: int = 1
    autobuild_index: bool = True      # construir el indice si falta
    autoextract: bool = True          # cortar extract de ciudad si no hay
    extract_margin_km: float = 15.0
    extract_round_deg: float = 0.1
    extract_max_km: float = 80.0
    osmium_bin: str = ""

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

            default_phone_region=_str("SMART_IMPORT_DEFAULT_PHONE_REGION", ""),
            delivery_accept_threshold=_float("SMART_IMPORT_DELIVERY_ACCEPT_THRESHOLD", 0.55),
            delivery_review_threshold=_float("SMART_IMPORT_DELIVERY_REVIEW_THRESHOLD", 0.30),
            address_accept_threshold=_float("SMART_IMPORT_ADDRESS_ACCEPT_THRESHOLD", 0.50),
            name_accept_threshold=_float("SMART_IMPORT_NAME_ACCEPT_THRESHOLD", 0.50),
            phone_accept_threshold=_float("SMART_IMPORT_PHONE_ACCEPT_THRESHOLD", 0.60),
            text_tabular_min_consistency=_float("SMART_IMPORT_TEXT_MIN_CONSISTENCY", 0.80),
            text_tabular_min_lines=_int("SMART_IMPORT_TEXT_MIN_LINES", 3),
            text_max_prose_ratio=_float("SMART_IMPORT_TEXT_MAX_PROSE_RATIO", 0.20),
            text_min_header_score=_float("SMART_IMPORT_TEXT_MIN_HEADER_SCORE", 0.50),

            address_parser=_str("SMART_IMPORT_ADDRESS_PARSER", "heuristic"),
            libpostal_enabled=_bool("SMART_IMPORT_LIBPOSTAL_ENABLED", False),


            geocoding_enabled=_bool("SMART_IMPORT_GEOCODING_ENABLED", True),
            pbf_dir=_resolve_pbf_dir(),
            index_dir=_str("SMART_IMPORT_INDEX_DIR", "data/indexes"),
            extract_dir=_str("SMART_IMPORT_EXTRACT_DIR", "data/extracts"),
            cache_path=_str("SMART_IMPORT_CACHE_PATH", "data/cache/geocode_cache.sqlite"),
            geocoder_fallback=_str("GEOCODER_FALLBACK", "none"),
            match_threshold=_float("GEOCODE_MATCH_THRESHOLD", 0.81),
            low_confidence_threshold=_float("GEOCODE_LOW_CONFIDENCE_THRESHOLD", 0.70),
            # UI bands (0–1 o 0–100). Fuente unica — la web NO redefine cortes.
            geocode_valid_band=_band("GEOCODE_VALID_BAND", "GEOCODE_VALID_MIN_PCT", 0.85),
            geocode_review_band=_band("GEOCODE_REVIEW_BAND", "GEOCODE_REVIEW_MIN_PCT", 0.75),
            geocode_street_level_floor=_float("GEOCODE_STREET_LEVEL_FLOOR", 0.60),
            geocode_street_match_min=_float("GEOCODE_STREET_MATCH_MIN", 0.70),
            geocode_soft_reject=_bool("GEOCODE_SOFT_REJECT", True),
            geocode_soft_reject_min=_float("GEOCODE_SOFT_REJECT_MIN", 0.70),
            max_low_confidence_km=_float("GEOCODE_MAX_LOW_CONFIDENCE_KM", 15.0),
            max_geocode_distance_km=_float("GEOCODE_MAX_DISTANCE_KM", 500.0),
            geocode_workers=_int("SMART_IMPORT_GEOCODE_WORKERS", 1),
            autobuild_index=_bool("SMART_IMPORT_AUTOBUILD_INDEX", True),
            autoextract=_bool("SMART_IMPORT_AUTOEXTRACT", True),
            extract_margin_km=_float("SMART_IMPORT_EXTRACT_MARGIN_KM", 15.0),
            extract_round_deg=_float("SMART_IMPORT_EXTRACT_ROUND_DEG", 0.1),
            extract_max_km=_float("SMART_IMPORT_EXTRACT_MAX_KM", 80.0),
            osmium_bin=_str("OSMIUM_BIN", ""),
        )

    def replace(self, **changes) -> "Config":
        current = {f.name: getattr(self, f.name) for f in fields(self)}
        current.update(changes)
        return Config(**current)

    def describe(self) -> dict:
        """Config efectiva, para /health y para el log de arranque."""
        return {f.name: getattr(self, f.name) for f in fields(self)}
