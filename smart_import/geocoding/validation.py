"""Validation shared by HTTP, queue workers and geocoding entry points."""
import math
from pathlib import Path


def validate_geo(origin=None, bbox=None, max_distance_km=None):
    if origin is not None:
        lat, lon = origin
        if (not math.isfinite(lat) or not math.isfinite(lon)
                or not -90 <= lat <= 90 or not -180 <= lon <= 180):
            raise ValueError("Coordenadas fuera de rango o no finitas")
    if bbox is not None:
        if len(bbox) != 4:
            raise ValueError("bbox espera north,south,east,west")
        n, s, e, w = bbox
        if (not all(math.isfinite(x) for x in bbox)
                or not (-90 <= s < n <= 90 and -180 <= w < e <= 180)):
            raise ValueError("bbox inválido; cruce del antimeridiano no soportado")
    if max_distance_km is not None and (
        not math.isfinite(max_distance_km) or max_distance_km <= 0
    ):
        raise ValueError("El radio debe ser finito y positivo")


def resolve_allowed_index(root, name, *, must_exist=True):
    base = Path(root).resolve()
    path = (base / name).resolve()
    if (Path(name).is_absolute() or not path.is_relative_to(base)
            or path == base or path.suffix != ".sqlite"):
        raise ValueError("Índice no permitido")
    if must_exist and not path.is_file():
        raise FileNotFoundError("Índice no disponible")
    return path
