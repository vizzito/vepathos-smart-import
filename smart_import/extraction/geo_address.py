"""Compatibilidad: `maximize_address_for_geocode` vive ahora en normalizacion.

Agregar "Buenos Aires" o "Argentina" a una direccion es NORMALIZAR, no extraer:
el extractor solo puede devolver lo que esta escrito en el texto. La funcion se
movio a `smart_import.normalization.address`; este modulo queda para el camino
legacy de columna compuesta (`extraction/heuristics.py`), que ya dependia de ella.
"""
from ..normalization.address import maximize_address_for_geocode

__all__ = ["maximize_address_for_geocode"]
