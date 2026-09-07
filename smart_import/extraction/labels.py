"""Etiquetas explicitas: `DIRECCION:` / `TE:` / `CONTACTO:` / `HORARIO:`.

Cuando el que escribio el mensaje ya dijo que campo es, no hay nada que adivinar:
la confianza es casi determinística. Las etiquetas viven en
`smart_import/resources/labels.json`, no en regex embebidas: agregar un idioma es
editar datos.

El valor de una etiqueta termina donde empieza la siguiente, o donde termina el
texto. Eso resuelve solo los mensajes encadenados:

    CONTACTO: Diego Martinez (1140351035). LUGAR: Darwin 1395. HORARIO: antes de 14hs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from ..resources import load_labels
from .result import FieldValue

#: grupo del labels.json -> campo del schema
LABEL_FIELDS = {
    "name_labels": "customer_name",
    "address_labels": "address",
    "phone_labels": "phone",
    "time_labels": "delivery_time_text",
    "reference_labels": "reference",
}

#: una etiqueta explicita es casi determinística
LABEL_CONFIDENCE = 0.95
#: basura de borde tipica cuando el valor quedo cortado por otra etiqueta
_EDGES = " \t\r\n.,;:|/\\-–—<>\"'"
#: cortes fuertes: el autor cambio de tema aunque no haya puesto otra etiqueta
_HARD_BREAK = re.compile(r"//|--|\s[-–—]\s")


@dataclass(frozen=True)
class LabelHit:
    field: str
    label: str
    value: str
    label_span: tuple[int, int]
    value_span: tuple[int, int]


@lru_cache(maxsize=8)
def _label_pattern(locales: tuple[str, ...] | None = None) -> re.Pattern[str]:
    """Una sola pasada para todas las etiquetas de todos los idiomas activos."""
    data = load_labels()
    wanted = set(locales) if locales else None
    by_label: dict[str, str] = {}
    for code, pack in data["locales"].items():
        if wanted is not None and code not in wanted:
            continue
        for group, target in LABEL_FIELDS.items():
            for label in pack.get(group, ()):
                # la etiqueta mas especifica gana si dos idiomas la comparten
                by_label.setdefault(label.lower(), target)

    alternatives = "|".join(
        re.escape(label) for label in sorted(by_label, key=len, reverse=True))
    return re.compile(
        rf"(?<![^\W\d_])(?P<label>{alternatives})\s*[:：]\s*",
        re.IGNORECASE | re.UNICODE,
    )


@lru_cache(maxsize=8)
def _label_targets(locales: tuple[str, ...] | None = None) -> dict[str, str]:
    data = load_labels()
    wanted = set(locales) if locales else None
    out: dict[str, str] = {}
    for code, pack in data["locales"].items():
        if wanted is not None and code not in wanted:
            continue
        for group, target in LABEL_FIELDS.items():
            for label in pack.get(group, ()):
                out.setdefault(label.lower(), target)
    return out


def _value_end(text: str, label_start: int, start: int, limit: int) -> int:
    """Donde termina el valor de una etiqueta.

    Ademas de la etiqueta siguiente cortan: un parentesis que cierra el grupo en
    el que vive la etiqueta —`(contact: 11-4054-1054)`— y los cortes fuertes
    `//` / `--`, que el autor usa como cambio de tema.
    """
    depth = text.count("(", 0, label_start) - text.count(")", 0, label_start)
    for i in range(start, limit):
        char = text[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return i
    match = _HARD_BREAK.search(text, start, limit)
    return match.start() if match else limit


_EMPTY_GROUP = re.compile(r"[(\[<]\s*[)\]>]")


def tidy(value: str) -> str:
    """Colapsa espacios y borra grupos que quedaron vacios al consumir un span."""
    cleaned = _EMPTY_GROUP.sub(" ", value or "")
    return re.sub(r"\s+", " ", cleaned).strip(_EDGES)


def _balance(value: str) -> str:
    """No dejar parentesis colgando en ninguno de los dos bordes."""
    while value.count("(") > value.count(")") and value.endswith("("):
        value = value[:-1].rstrip(_EDGES)
    while value.count(")") > value.count("(") and value.startswith(")"):
        value = value[1:].lstrip(_EDGES)
    if value.count("(") > value.count(")") and "(" in value:
        value = value[:value.rindex("(")].rstrip(_EDGES)
    if value.count(")") > value.count("("):
        value = value[:value.rindex(")")].rstrip(_EDGES)
    return value


def find_labels(text: str, locales: tuple[str, ...] | None = None) -> list[LabelHit]:
    """Etiquetas con su valor, recortado en la siguiente etiqueta."""
    if not text:
        return []
    pattern = _label_pattern(locales)
    targets = _label_targets(locales)
    matches = list(pattern.finditer(text))
    hits: list[LabelHit] = []

    for i, match in enumerate(matches):
        start = match.end()
        limit = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        end = _value_end(text, match.start(), start, limit)
        raw = text[start:end]
        # `trimmed` sigue siendo un trozo literal de `raw` (solo se recorto), asi
        # que sirve para el span; `value` ya paso por tidy y puede no serlo.
        trimmed = _balance(raw.strip(_EDGES))
        value = tidy(trimmed)
        if not value or not trimmed:
            continue
        offset = start + raw.index(trimmed)
        label = match.group("label")
        hits.append(LabelHit(
            field=targets[label.lower()],
            label=label,
            value=value,
            label_span=match.span(),
            value_span=(offset, offset + len(trimmed)),
        ))
    return hits


def extract_labeled(canvas, context) -> list[FieldValue]:
    """Todos los campos que el autor del mensaje etiqueto explicitamente."""
    out: list[FieldValue] = []
    for hit in find_labels(canvas.remaining(), context.locales):
        out.append(FieldValue(
            field=hit.field,
            value=hit.value,
            raw=canvas.slice(hit.value_span),
            confidence=LABEL_CONFIDENCE,
            method="explicit_label",
            span=hit.value_span,
            evidence=(f"etiqueta explicita '{hit.label}'",),
        ))
    return out
