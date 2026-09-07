"""Coordenadas explicitas en el texto. Si estan, el geocoding sobra.

Se aceptan solo pares completos y dentro de rango. Un numero suelto que "parece"
una latitud no alcanza: mandar una entrega a otro pais por un decimal mal leido
es peor que pedir geocoding.
"""
from __future__ import annotations

import re

from .result import FieldValue

_NUM = r"[-+]?\d{1,3}(?:[.,]\d{3,12})"
_LAT_LABEL = r"lat(?:itude|itud)?"
_LON_LABEL = r"lo?n(?:g(?:itude|itud)?)?"

# lat: -34.6037, lng: -58.3816   |   latitud=-34.6 longitud=-58.3
_LABELED = re.compile(
    rf"(?<![^\W\d_]){_LAT_LABEL}\s*[:=]?\s*(?P<lat>{_NUM})"
    rf"[\s,;/|]+(?:{_LON_LABEL})?\s*[:=]?\s*(?P<lon>{_NUM})",
    re.IGNORECASE,
)
# par pelado entre parentesis o separado por coma: (-34.6037, -58.3816)
_BARE = re.compile(rf"(?<![\w.]) (?P<lat>{_NUM}) \s*,\s* (?P<lon>{_NUM}) (?![\w.])",
                   re.IGNORECASE | re.VERBOSE)


def _to_float(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except (TypeError, ValueError):
        return None


def _in_range(lat: float | None, lon: float | None) -> bool:
    return (lat is not None and lon is not None
            and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0)


def find_coordinates(text: str) -> tuple[float, float, tuple[int, int], str] | None:
    """(lat, lon, span, metodo) del primer par valido, o None."""
    for pattern, method in ((_LABELED, "labeled_coords"), (_BARE, "coord_pair")):
        for match in pattern.finditer(text or ""):
            lat, lon = _to_float(match.group("lat")), _to_float(match.group("lon"))
            if _in_range(lat, lon):
                return lat, lon, match.span(), method
    return None


def extract_coordinates(canvas, context) -> list[FieldValue]:
    found = find_coordinates(canvas.remaining())
    if not found:
        return []
    lat, lon, span, method = found
    evidence = ("par lat/lng completo", "dentro de rango")
    confidence = 0.99 if method == "labeled_coords" else 0.85
    return [
        FieldValue("lat", lat, canvas.slice(span), confidence, method, span, evidence),
        FieldValue("lng", lon, canvas.slice(span), confidence, method, span, evidence),
    ]
