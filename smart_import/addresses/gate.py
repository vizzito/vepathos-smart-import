"""Cuando vale la pena pedir ayuda a un enhancer (libpostal u otro).

El heuristico resuelve LatAm y EN tipico. El enhancer solo se consulta si el
resultado es incompleto o la `road` parece un falso positivo (parcela, manzana,
lote…). Asi el camino caliente no paga red/CPU por direcciones ya bien parseadas.
"""
from __future__ import annotations

from ..resources import (
    fold, label_set, suspicious_road_prefixes, suspicious_road_words,
)
from .base import ParsedAddress


def road_is_suspicious(road: str, locales: tuple[str, ...] | None = None) -> bool:
    """True cuando `road` no parece un nombre de via utilizable para geocoding."""
    cleaned = (road or "").strip(" .,;:-")
    if not cleaned:
        return True
    folded = fold(cleaned)
    if folded in suspicious_road_words():
        return True
    if any(folded.startswith(fold(p)) for p in suspicious_road_prefixes()):
        return True
    # Una sola palabra que es token de via ('Calle', 'Road') sin nombre propio.
    tokens = label_set("street_tokens", locales)
    words = [fold(w.strip(".,;:")) for w in cleaned.split() if w.strip(".,;:")]
    if len(words) == 1 and words[0] in tokens:
        return True
    return False


def needs_enhancement(parsed: ParsedAddress,
                      locales: tuple[str, ...] | None = None) -> bool:
    """True = conviene consultar el enhancer; False = el heuristico alcanza."""
    return enhancement_reason(parsed, locales) is not None


def enhancement_reason(parsed: ParsedAddress,
                       locales: tuple[str, ...] | None = None) -> str | None:
    """Por que se pediria enhancer, o None si no hace falta.

    Razones estables (tests / metricas / logs):
      * missing_road     — no hay calle
      * suspicious_road  — hay 'road' pero no es confiable
    """
    road = (parsed.get("road") or "").strip()
    if not road:
        return "missing_road"
    if road_is_suspicious(road, locales):
        return "suspicious_road"
    return None
