"""Aisla la direccion dentro de una frase, sin quedarse con la frase entera.

    "Ana Perez paso a avisar que vive en Av. Corrientes 100 en CABA y el cel..."
                                        └──────── esto ────────┘

Se ancla en el par calle+altura que encuentra el `AddressParser` y se expande a
los costados hasta el borde de la clausula. Nunca inventa localidad ni pais: eso
es trabajo del normalizador, no del extractor.
"""
from __future__ import annotations

import re

from ..addresses.heuristic import find_street_span
from ..addresses.scoring import AddressCandidateScorer
from .labels import tidy
from .result import FieldValue

#: cortes de clausula: donde el autor cambio de tema
_BOUNDARY = re.compile(
    r"\.\s|\.$|;|\||//|--|\s[-–—]\s|\s(?:y|e|and|pero|but)\s|!|\?|\n",
    re.IGNORECASE)
#: conectores que suelen quedar colgando en los bordes
_LEAD = re.compile(r"^(?:en|a|al|the|de|do|da|na|no|at|in)\s+", re.IGNORECASE)
_TRAIL = re.compile(r"\s+(?:y|e|and|de|el|la|los|las|the)$", re.IGNORECASE)
#: cuantas palabras puede tener un segmento de barrio/ciudad antes de ser prosa
MAX_LOCALITY_WORDS = 4


def _clause_end(text: str, start: int) -> int:
    match = _BOUNDARY.search(text, start)
    return match.start() if match else len(text)


def _clause_start(text: str, end: int) -> int:
    last = 0
    for match in _BOUNDARY.finditer(text, 0, end):
        last = match.end()
    return last


def extract_address(canvas, context, scorer: AddressCandidateScorer | None = None,
                    parser=None) -> FieldValue | None:
    """La direccion mas creible del texto restante, o None si no hay evidencia."""
    text = canvas.remaining()
    span = find_street_span(text, context.locales)
    scorer = scorer or AddressCandidateScorer(context.locales)

    if span is None:
        return _fallback(canvas, text, scorer, parser, context)

    start, end = span
    start = _expand_left(text, start, _clause_start(text, start), context)
    end = _expand_right(text, end, _clause_end(text, end), context)
    candidate = tidy(text[start:end])
    candidate = _TRAIL.sub("", _LEAD.sub("", candidate)).strip(" .,;:-")
    if not candidate:
        return None

    parsed = parser.parse(candidate, context) if parser else None
    result = scorer.score(parsed or candidate)
    real_span = canvas.find(candidate) or (start, start + len(candidate))
    evidence = list(result.evidence)
    if parsed and parsed.components:
        evidence.append(f"{parsed.parser}: {parsed.normalized}")
    return FieldValue("address", candidate, canvas.slice(real_span), result.score,
                      f"street_anchor+{parsed.parser if parsed else 'rules'}",
                      real_span, tuple(evidence))


def _expand_left(text: str, start: int, floor: int, context) -> int:
    """Recupera el token de unidad que precede al ancla: 'Flat 14B', 'piso 3'."""
    from ..resources import fold, label_set

    tokens = label_set("street_tokens", context.locales)
    head = text[floor:start].rstrip()
    if not head:
        return start
    word = head.split()[-1].strip(".,;:")
    if fold(word) not in tokens:
        return start
    return floor + head.rindex(word)


def _expand_right(text: str, start: int, limit: int, context) -> int:
    """Suma barrio/ciudad despues de la altura, y frena cuando arranca la prosa.

    'Av. Cabildo 174, llamar al 11-4002-1002 si no contesta' corta en la coma;
    'Malabia 1136, Palermo' se la queda. La diferencia es si el segmento parece
    un lugar (corto, capitalizado o con token de via) o una oracion.
    """
    from ..resources import fold, label_set

    tokens = label_set("street_tokens", context.locales)
    end = start
    position = start
    while position < limit:
        cut = text.find(",", position)
        stop = min(cut if cut >= 0 else limit, limit)
        segment = text[position:stop]
        cleaned = _LEAD.sub("", segment.strip(" .,;:-()[]"))
        words = cleaned.split()
        if not words:
            position = stop + 1
            continue
        head = words[0].strip(".,;:")
        if len(words) > MAX_LOCALITY_WORDS or not (
                head[:1].isupper() or fold(head) in tokens):
            break
        end = stop
        position = stop + 1
    return max(end, start)


def _fallback(canvas, text: str, scorer, parser, context) -> FieldValue | None:
    """Sin calle+altura: se prueba clausula por clausula y gana la mejor puntuada."""
    best: FieldValue | None = None
    position = 0
    while position < len(text):
        end = _clause_end(text, position)
        chunk = tidy(text[position:end])
        position = max(end + 1, position + 1)
        if len(chunk.split()) < 2:
            continue
        for variant in _address_variants(chunk):
            parsed = parser.parse(variant, context) if parser else None
            result = scorer.score(parsed or variant)
            if best is not None and result.score < best.confidence:
                continue
            # Empate: preferir la variante mas corta (no tragarse el nombre).
            if (best is not None and result.score == best.confidence
                    and len(variant) >= len(best.value)):
                continue
            span = canvas.find(variant) or (0, 0)
            best = FieldValue("address", variant, variant, result.score,
                              "clause_scoring", span, tuple(result.evidence))
    return best


_LEADING_NAME = re.compile(
    r"^([A-ZÁÉÍÓÚÜÑ][^\W\d_]{1,20}(?:\s+[A-ZÁÉÍÓÚÜÑ][^\W\d_]{1,20}){1,3})"
    r",\s+(.+)$", re.UNICODE)


def _address_variants(chunk: str) -> tuple[str, ...]:
    """Prueba la clausula entera y, si aplica, sin un nombre de persona al frente.

    'Rahul Sharma, Flat 14B, Shanti Nagar, Mumbai 400069' tiene que dejar el
    nombre afuera del address para que `extract_customer_name` lo encuentre.
    """
    match = _LEADING_NAME.match(chunk)
    if not match or len(match.group(2).split()) < 2:
        return (chunk,)
    return (chunk, match.group(2))
