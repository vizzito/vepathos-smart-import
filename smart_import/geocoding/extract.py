"""Corta un extract de ciudad desde un PBF de país/región.

Smart Import no escribe en `_extracts` del cutter (en Docker va :ro). El
recorte vive en `SMART_IMPORT_EXTRACT_DIR` (default `data/extracts`) con el
mismo nombre `n…_s…_e…_w…-pyrosm.osm.pbf` para que el índice se llame igual.

Si ya hay extract o índice que cubre el área, no se corta nada. Si solo hay
PBF de país, se llama a `osmium extract` y se indexa el recorte — nunca el
archivo de 400 MB.
"""
from __future__ import annotations

import fcntl
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..logging_setup import get_logger, stage
from .osm_index import (
    build,
    covering_extract_index,
    index_path_for,
)
from .pbf_registry import PbfEntry, PbfRegistry, _parse

logger = get_logger("extract")

KM_PER_DEG_LAT = 111.32
MIN_USEFUL_PBF_BYTES = 32 * 1024
_ZONE_UNSAFE = re.compile(r"[^a-zA-Z0-9._-]+")
_RESERVED_ZONE = {"", ".", "..", "_extracts", "extracts", "data", "indexes", ".locks"}


class ExtractError(RuntimeError):
    """No se pudo cortar el extract (nótese: no se cae al índice de país)."""


def km_per_deg_lon(lat: float) -> float:
    return KM_PER_DEG_LAT * max(0.2, math.cos(math.radians(lat)))


def expand_bbox_km(
    west: float, south: float, east: float, north: float, margin_km: float,
) -> tuple[float, float, float, float]:
    if margin_km <= 0:
        return west, south, east, north
    center_lat = (north + south) / 2.0
    dlat = margin_km / KM_PER_DEG_LAT
    dlon = margin_km / km_per_deg_lon(center_lat)
    return west - dlon, south - dlat, east + dlon, north + dlat


def clamp_bbox_max_km(
    west: float, south: float, east: float, north: float, max_km: float,
    center_lat: float | None = None, center_lon: float | None = None,
) -> tuple[float, float, float, float]:
    """Si el span supera max_km, centra (depot si hay) y recorta."""
    mid_lat = (north + south) / 2.0
    height = abs(north - south) * KM_PER_DEG_LAT
    width = abs(east - west) * km_per_deg_lon(mid_lat)
    if height <= max_km and width <= max_km:
        return west, south, east, north
    lat = mid_lat if center_lat is None else center_lat
    lon = ((east + west) / 2.0) if center_lon is None else center_lon
    half = max_km / 2.0
    dlat = half / KM_PER_DEG_LAT
    dlon = half / km_per_deg_lon(lat)
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def round_bbox_outward(
    west: float, south: float, east: float, north: float, step: float,
) -> tuple[float, float, float, float]:
    step = max(step, 1e-6)
    decimals = max(0, -int(math.floor(math.log10(step)))) + 1

    def _floor(value: float) -> float:
        return round(math.floor(value / step + 1e-9) * step, decimals)

    def _ceil(value: float) -> float:
        return round(math.ceil(value / step - 1e-9) * step, decimals)

    return _floor(west), _floor(south), _ceil(east), _ceil(north)


def prepare_extract_bbox(
    west: float, south: float, east: float, north: float,
    *,
    margin_km: float = 15.0,
    max_km: float = 80.0,
    round_deg: float = 0.1,
    center_lat: float | None = None,
    center_lon: float | None = None,
) -> tuple[float, float, float, float]:
    w, s, e, n = expand_bbox_km(west, south, east, north, margin_km)
    w, s, e, n = clamp_bbox_max_km(
        w, s, e, n, max_km, center_lat=center_lat, center_lon=center_lon,
    )
    return round_bbox_outward(w, s, e, n, max(round_deg, 0.001))


def target_bbox_wsen(
    lat: float | None,
    lon: float | None,
    bbox: tuple[float, float, float, float] | None,
    *,
    margin_km: float = 15.0,
    max_km: float = 80.0,
    round_deg: float = 0.1,
) -> tuple[float, float, float, float]:
    """Convierte punto y/o bbox (N,S,E,W) al recorte osmium (W,S,E,N)."""
    if bbox is not None:
        north, south, east, west = bbox
    elif lat is not None and lon is not None:
        north = south = lat
        east = west = lon
    else:
        raise ExtractError("hace falta un punto o un bbox para cortar el extract")
    return prepare_extract_bbox(
        west, south, east, north,
        margin_km=margin_km, max_km=max_km, round_deg=round_deg,
        center_lat=lat, center_lon=lon,
    )


def filename_decimals(round_deg: float = 0.1) -> int:
    step = max(round_deg, 1e-6)
    return max(2, math.ceil(-math.log10(step)))


def bbox_filename(
    west: float, south: float, east: float, north: float,
    round_deg: float = 0.1,
) -> str:
    d = filename_decimals(round_deg)
    return (
        f"n{north:.{d}f}_s{south:.{d}f}_e{east:.{d}f}_w{west:.{d}f}-pyrosm.osm.pbf"
    )


def extract_zone(source_path: str | Path, country_slug: str | None = None) -> str:
    """Carpeta del extract: slug del país/región, o la carpeta del PBF fuente."""
    if country_slug:
        zone = _ZONE_UNSAFE.sub("-", country_slug).strip(".-").lower()
        if zone and zone not in _RESERVED_ZONE:
            return zone
    parent = Path(source_path).resolve().parent.name
    zone = _ZONE_UNSAFE.sub("-", parent).strip(".-").lower()
    if zone in _RESERVED_ZONE:
        return "misc"
    return zone or "misc"


def is_useful_pbf(path: str | Path, min_bytes: int = MIN_USEFUL_PBF_BYTES) -> bool:
    try:
        p = Path(path)
        return p.is_file() and p.stat().st_size >= max(1, min_bytes)
    except OSError:
        return False


def find_osmium(explicit: str = "") -> str | None:
    """Binario de osmium-tool. None si no está instalado."""
    candidates: list[str] = []
    if explicit.strip():
        candidates.append(explicit.strip())
    env = os.getenv("OSMIUM_BIN", "").strip()
    if env:
        candidates.append(env)
    which = shutil.which("osmium")
    if which:
        candidates.append(which)
    candidates.append("/opt/homebrew/bin/osmium")
    candidates.append("/usr/bin/osmium")
    seen: set[str] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        path = Path(cand)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def scan_registry(pbf_dir: str | Path, extract_dir: str | Path | None = None) -> PbfRegistry:
    """País/región en pbf_dir + extracts propios, sin duplicar paths."""
    entries: list[PbfEntry] = []
    seen: set[str] = set()
    for root in (pbf_dir, extract_dir):
        if not root:
            continue
        for entry in PbfRegistry.scan(root).entries:
            key = str(entry.path.resolve()) if entry.path.exists() else str(entry.path)
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
    return PbfRegistry(entries)


def run_osmium_extract(
    source_path: str | Path,
    dest_path: str | Path,
    west: float, south: float, east: float, north: float,
    *,
    osmium_bin: str | None = None,
) -> None:
    binary = find_osmium(osmium_bin or "")
    if binary is None:
        raise ExtractError(
            "falta osmium-tool para cortar el extract. "
            "Instalálo (`brew install osmium-tool` o el paquete osmium-tool) "
            "o dejá un extract en SMART_IMPORT_EXTRACT_DIR / _extracts. "
            "No se indexa el PBF de país entero."
        )
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # osmium infiere el formato por extensión: el temporal DEBE terminar en .osm.pbf
    dest_s = str(dest)
    if dest_s.endswith(".osm.pbf"):
        tmp = Path(dest_s[:-len(".osm.pbf")] + f".tmp.{os.getpid()}.osm.pbf")
    else:
        tmp = dest.with_name(f"{dest.name}.tmp.{os.getpid()}.osm.pbf")
    cmd = [
        binary, "extract",
        "-b", f"{west},{south},{east},{north}",
        str(source_path),
        "-o", str(tmp),
        "--overwrite",
    ]
    stage(logger, "GEOCODE", "cortando extract con osmium",
          fuente=Path(source_path).name, destino=dest.name,
          bbox=f"{west:.4f},{south:.4f},{east:.4f},{north:.4f}")
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise ExtractError(f"osmium exit {proc.returncode}: {err[:500]}")
        if not tmp.is_file():
            raise ExtractError("osmium no produjo el archivo de extract")
        size = tmp.stat().st_size
        if size < MIN_USEFUL_PBF_BYTES:
            tmp.unlink(missing_ok=True)
            raise ExtractError(
                f"osmium extract inútil ({size} bytes < {MIN_USEFUL_PBF_BYTES}); "
                "la fuente no tiene datos en este bbox"
            )
        os.replace(tmp, dest)
        stage(logger, "GEOCODE", "extract listo",
              archivo=dest.name,
              tamano=f"{dest.stat().st_size / 1e6:.1f}MB",
              t=f"{time.time() - t0:.1f}s")
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def ensure_extract(
    source_path: str | Path,
    west: float, south: float, east: float, north: float,
    extract_dir: str | Path,
    *,
    country_slug: str | None = None,
    round_deg: float = 0.1,
    osmium_bin: str | None = None,
) -> Path:
    """Garantiza el .osm.pbf del bbox en extract_dir/<zona>/ (con file lock)."""
    zone = extract_zone(source_path, country_slug)
    dest = Path(extract_dir).expanduser() / zone / bbox_filename(
        west, south, east, north, round_deg,
    )
    if is_useful_pbf(dest):
        stage(logger, "GEOCODE", "extract reutilizado", archivo=dest.name)
        return dest
    if dest.is_file():
        dest.unlink(missing_ok=True)

    dest.parent.mkdir(parents=True, exist_ok=True)
    lock_dir = dest.parent / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{dest.name}.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        if is_useful_pbf(dest):
            return dest
        run_osmium_extract(
            source_path, dest, west, south, east, north, osmium_bin=osmium_bin,
        )
    return dest


@dataclass
class ReadyIndex:
    path: Path
    entry: PbfEntry
    country_slug: str | None
    cut_extract: bool = False
    built_index: bool = False


def ensure_geocode_index(
    pbf_dir: str | Path,
    index_dir: str | Path,
    *,
    lat: float | None = None,
    lon: float | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    zone_hint: str | None = None,
    extract_dir: str | Path | None = None,
    autobuild: bool = True,
    autoextract: bool = True,
    margin_km: float = 15.0,
    max_km: float = 80.0,
    round_deg: float = 0.1,
    osmium_bin: str | None = None,
    progress=None,
) -> ReadyIndex:
    """Elige (o corta + indexa) el extract. Nunca construye argentina.sqlite."""
    extracts = extract_dir or (Path(index_dir).expanduser().parent / "extracts")
    registry = scan_registry(pbf_dir, extracts)
    entry = registry.resolve(lat=lat, lon=lon, bbox=bbox, zone_hint=zone_hint)
    if entry is None:
        raise FileNotFoundError(
            f"sin cobertura PBF para el area pedida en {pbf_dir} "
            f"({len(registry.entries)} PBF disponibles)"
        )

    lat_i = lat if lat is not None else (bbox[0] + bbox[1]) / 2 if bbox else None
    lon_i = lon if lon is not None else (bbox[2] + bbox[3]) / 2 if bbox else None

    if lat_i is not None and lon_i is not None:
        leftover = covering_extract_index(index_dir, lat_i, lon_i, bbox)
        if leftover is not None:
            stage(logger, "GEOCODE", "indice de extract reutilizado",
                  indice=leftover.name)
            return ReadyIndex(
                path=leftover, entry=entry,
                country_slug=entry.country_slug, cut_extract=False,
            )

    if entry.has_bbox:
        index = index_path_for(entry, index_dir)
        built = False
        if not index.exists():
            if not autobuild:
                raise FileNotFoundError(
                    f"falta el indice {index.name} y SMART_IMPORT_AUTOBUILD_INDEX "
                    "esta en false. Generalo con 'build-geocoder-index'."
                )
            if progress:
                progress("building_index", pbf=entry.path.name)
            build(entry.path, index)
            built = True
        return ReadyIndex(
            path=index, entry=entry,
            country_slug=entry.country_slug, built_index=built,
        )

    # PBF de país/región: cortar extract. No indexar el archivo entero.
    if not autoextract:
        raise FileNotFoundError(
            f"sin extract que cubra el area y SMART_IMPORT_AUTOEXTRACT=false. "
            f"No se indexa {entry.path.name} entero."
        )
    if lat_i is None or lon_i is None:
        raise FileNotFoundError(
            f"el PBF {entry.path.name} es de pais/region; hace falta un punto "
            "o bbox para cortar el extract"
        )
    if not autobuild:
        raise FileNotFoundError(
            f"falta extract e indice para el area y SMART_IMPORT_AUTOBUILD_INDEX "
            "esta en false."
        )

    west, south, east, north = target_bbox_wsen(
        lat_i, lon_i, bbox,
        margin_km=margin_km, max_km=max_km, round_deg=round_deg,
    )
    if progress:
        progress("cutting_extract", pbf=entry.path.name)
    dest = ensure_extract(
        entry.path, west, south, east, north, extracts,
        country_slug=entry.country_slug, round_deg=round_deg,
        osmium_bin=osmium_bin,
    )
    cut_entry = _parse(dest)
    index = index_path_for(cut_entry, index_dir)
    built = False
    if not index.exists():
        if progress:
            progress("building_index", pbf=dest.name)
        build(dest, index)
        built = True
    stage(logger, "GEOCODE", "PBF elegido",
          pbf=dest.name, pais=entry.country_slug, extract=cut_entry.key)
    return ReadyIndex(
        path=index, entry=cut_entry, country_slug=entry.country_slug,
        cut_extract=True, built_index=built,
    )


def ensure_geocode_index_from_config(
    cfg,
    *,
    lat: float | None = None,
    lon: float | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    zone_hint: str | None = None,
    pbf_dir: str | Path | None = None,
    index_dir: str | Path | None = None,
    progress=None,
) -> ReadyIndex:
    """Misma resolución para CLI, API y accuracy."""
    return ensure_geocode_index(
        pbf_dir=pbf_dir or cfg.pbf_dir,
        index_dir=index_dir or cfg.index_dir,
        lat=lat, lon=lon, bbox=bbox, zone_hint=zone_hint,
        extract_dir=cfg.extract_dir,
        autobuild=cfg.autobuild_index,
        autoextract=cfg.autoextract,
        margin_km=cfg.extract_margin_km,
        max_km=cfg.extract_max_km,
        round_deg=cfg.extract_round_deg,
        osmium_bin=cfg.osmium_bin,
        progress=progress,
    )
