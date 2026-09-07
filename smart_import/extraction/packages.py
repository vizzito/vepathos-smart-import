"""Bultos, peso y medidas. Siempre con unidad o sustantivo de contexto.

Un numero suelto no es una cantidad: '11 de Septiembre 1913' tiene dos numeros y
ningun bulto. Se exige la palabra (bultos/paquetes/kg) o la forma inequivoca
(20x30x40) para no convertir direcciones y telefonos en cantidades.

Los spans se ensanchan a la izquierda para consumir etiquetas (`qty:`, `peso`,
`dimensions`, …) antes de que el extractor de address las trate como calle.
"""
from __future__ import annotations

import re

from .result import FieldValue

# 2 bultos | 3 paquetes | 5 volumes | qty: 4
_QUANTITY = re.compile(
    r"(?<!\d)(?P<n>\d{1,3})\s*(?P<word>bultos?|paquetes?|cajas?|piezas?|unidades?|"
    r"packages?|parcels?|boxes|pieces|volumes?|pacotes?)\b",
    re.IGNORECASE)
# 8 kg | 8,5 kg | 800 g | 2 lb
_WEIGHT = re.compile(
    r"(?<!\d)(?P<n>\d{1,4}(?:[.,]\d{1,3})?)\s*(?P<unit>kgs?|kilos?|kg\.|g|grs?|gramos?|lbs?)\b",
    re.IGNORECASE)
# 20x30x40 | 20 x 30 x 40 cm | 20 X 30 X 40
_DIMENSIONS = re.compile(
    r"(?<!\d)(?P<l>\d{1,4}(?:[.,]\d{1,2})?)\s*[x×]\s*(?P<w>\d{1,4}(?:[.,]\d{1,2})?)"
    r"\s*[x×]\s*(?P<h>\d{1,4}(?:[.,]\d{1,2})?)\s*(?P<unit>cm|mm|m|in|\")?",
    re.IGNORECASE)

# Etiquetas sueltas a la izquierda del match (no forman parte del valor).
_LABEL_LEFT = re.compile(
    r"(?:qty|quantity|cantidad|cant\.?|peso|weight|wt|"
    r"medidas?|dimensions?|dims?|size|tama[ñn]o)\s*[:=]?\s*$",
    re.IGNORECASE)

_TO_KG = {"kg": 1.0, "kgs": 1.0, "kilo": 1.0, "kilos": 1.0, "kg.": 1.0,
          "g": 0.001, "gr": 0.001, "grs": 0.001, "gramo": 0.001, "gramos": 0.001,
          "lb": 0.45359237, "lbs": 0.45359237}
_TO_CM = {"cm": 1.0, "mm": 0.1, "m": 100.0, "in": 2.54, '"': 2.54, None: 1.0}


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except (TypeError, ValueError):
        return None


def widen_package_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Incluye etiquetas `qty:` / `peso` / `dimensions` a la izquierda del match."""
    while start > 0:
        left = start
        while left > 0 and text[left - 1].isspace():
            left -= 1
        match = _LABEL_LEFT.search(text[:left])
        if not match:
            break
        start = match.start()
    return start, end


def extract_packages(canvas, context) -> list[FieldValue]:
    text = canvas.remaining()
    out: list[FieldValue] = []

    if match := _DIMENSIONS.search(text):
        factor = _TO_CM.get((match.group("unit") or "").lower() or None, 1.0)
        dims = [_number(match.group(k)) for k in ("l", "w", "h")]
        if all(d is not None for d in dims):
            raw = match.group(0)
            span = widen_package_span(text, *match.span())
            for name, value in zip(("length_cm", "width_cm", "height_cm"), dims):
                out.append(FieldValue(name, round(value * factor, 2), raw, 0.92,
                                      "dimensions", span,
                                      (f"medidas '{raw}'",)))

    if match := _WEIGHT.search(text):
        value = _number(match.group("n"))
        factor = _TO_KG.get(match.group("unit").lower())
        if value is not None and factor:
            span = widen_package_span(text, *match.span())
            out.append(FieldValue("weight_kg", round(value * factor, 3), match.group(0),
                                  0.92, "weight", span,
                                  (f"peso con unidad '{match.group('unit')}'",
                                   "en texto libre el peso es TOTAL de la entrega")))

    if match := _QUANTITY.search(text):
        value = _number(match.group("n"))
        if value is not None:
            span = widen_package_span(text, *match.span())
            out.append(FieldValue("quantity", int(value), match.group(0), 0.90,
                                  "quantity", span,
                                  (f"cantidad con sustantivo '{match.group('word')}'",)))
    return out


def has_package_signal(text: str) -> bool:
    """Evidencia logistica para el clasificador de candidatos."""
    return bool(_QUANTITY.search(text or "") or _WEIGHT.search(text or "")
                or _DIMENSIONS.search(text or ""))
