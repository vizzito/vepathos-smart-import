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


M3_TO_CM3 = 1_000_000

#: la columna declara metros cubicos ('m3', 'cbm', 'metros cubicos'): el schema es cm3
_CUBIC_METER_TOKENS = frozenset({"m3", "m³", "cbm", "mc", "metros cubicos", "metro cubico",
                                  "cubic meters", "cubic metres", "cubic meter"})


def column_declares_cubic_meters(column: str) -> bool:
    """True si el header de volumen esta en m3. 'volume_cm3' / 'cm3' nunca convierten."""
    n = normalize_key(column)
    if not n:
        return False
    compact = n.replace(" ", "")
    if "cm3" in compact or "cc" in n.split() or "litro" in compact or "liter" in compact:
        return False
    if n in _CUBIC_METER_TOKENS or compact in {t.replace(" ", "") for t in _CUBIC_METER_TOKENS}:
        return True
    tokens = set(n.split())
    return bool(tokens & {"m3", "m³", "cbm"}) or "metroscubicos" in compact


def cubic_meters_to_cm3(value: float) -> float:
    return round(float(value) * M3_TO_CM3, 3)


#: headers que ya vienen en centavos: no se multiplican
_CENTS_MARKERS = ("cent", "centavo", "cents", "minor")


def column_declares_major_currency(column: str) -> bool:
    """'valor' / 'importe' / 'amount' son unidades de moneda; el schema guarda centavos.

    Un export con 'Valor: 15000' (pesos) entraba como value_cents=15000 → $150.
    Solo un header que diga centavos se toma tal cual.
    """
    n = normalize_key(column)
    if not n:
        return False
    return not any(marker in n.replace(" ", "") for marker in _CENTS_MARKERS)


def major_to_cents(value: float) -> int:
    return int(round(float(value) * 100))
