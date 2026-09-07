"""Conversiones de unidades al normalizar columnas mapeadas a schema Vepathos.

El schema canónico es siempre kg / cm. Si la columna origen declara libras
(alias `weight_lb`, `lbs`, …), se convierte a `weight_kg` acá — no en el mapper.
"""
from __future__ import annotations

from ..schemas import normalize_key

LB_TO_KG = 0.45359237

# aliases normalizados que implican libras (no kg). "weight" solo NO convierte.
_POUND_MARKERS = frozenset({
    "lb", "lbs", "pound", "pounds", "libra", "libras",
    "weight lb", "weight lbs",
    "peso lb", "peso lbs", "peso libras",
    "wt lb", "wt lbs",
    "gross weight lb", "gross weight lbs",
})


def column_declares_pounds(column: str) -> bool:
    """True si el nombre de columna indica libras (no kilogramos)."""
    n = normalize_key(column)
    if not n:
        return False
    compact = n.replace(" ", "")
    # Si el header menciona kg, nunca convertir (p.ej. "weight kg / lb" raro → kg gana)
    if "kg" in compact or "kilo" in compact:
        return False
    if n in _POUND_MARKERS:
        return True
    tokens = set(n.split())
    if tokens & {"lb", "lbs", "pound", "pounds", "libra", "libras"}:
        return True
    return compact in {
        "weightlb", "weightlbs", "pesolb", "pesolbs", "wtlb", "wtlbs",
        "pound", "pounds", "lb", "lbs", "libra", "libras",
    }


def pounds_to_kg(value: float) -> float:
    return round(float(value) * LB_TO_KG, 6)
