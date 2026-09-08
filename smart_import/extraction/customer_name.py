"""El nombre del cliente, DESPUES de resolver telefono, direccion y etiquetas.

El nombre es el campo menos objetivo: se resuelve ultimo, cuando lo demas ya
salio del texto y queda mucha menos ambiguedad. Tres reglas, en orden de cuanta
evidencia tienen:

  1. prefijo explicito       'Para Maria Gomez'          -> Maria Gomez   0.90
  2. racha capitalizada      'Ana Perez vive en...'      -> Ana Perez     0.75
  3. primer campo del renglon 'martin vizzolini, av...'  -> martin…       0.60

El prefijo NUNCA queda adentro del valor: 'Para', 'Destinatario:' y 'CONTACTO:'
son etiquetas, no parte del nombre.

La regla 3 existe porque media humanidad escribe sin mayusculas en el celular, y
"no hay nombre" es peor que "hay un nombre con menos confianza". Pero pide
estructura a cambio: tiene que ser el PRIMER campo de un renglon con separadores
—`martin vizzolini, av santa fe 890, palermo`—, no una frase suelta. Sin esa
condicion, 'dejar en porteria' se convierte en un cliente.

La capitalizacion se pregunta en Python (`str.isupper()`), no con una clase de
regex: `[A-ZÁÉÍÓÚÜÑ]` es el alfabeto castellano y descarta en silencio a Ângela,
Öztürk, Łukasz y Đorđe.
"""
from __future__ import annotations

import re

from ..resources import fold, label_set, name_glue_words
from .result import FieldValue

#: palabras del texto, sin numeros: el token de un nombre nunca tiene digitos
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
#: separadores de campo de un renglon escrito a mano
_FIELD_SPLIT = re.compile(r"\s*[,;|/]\s*|\s+[-–—]\s+")

MIN_NAME_CHARS = 4
MAX_NAME_WORDS = 4
#: conectores seguidos que aguanta un nombre ('Maria de los Angeles Perez')
MAX_GLUE_RUN = 2
#: palabras minimas y maximas del campo que se acepta como nombre en minuscula
MIN_LOWER_WORDS = 2
MAX_LOWER_WORDS = 3

CONF_PREFIX = 0.90
CONF_CAPITALIZED = 0.75
#: sin mayusculas la evidencia es posicional, no lexica: pide revision humana
CONF_LOWERCASE = 0.60


def _prefix_pattern(locales: tuple[str, ...] | None) -> re.Pattern[str]:
    prefixes = sorted(label_set("name_prefixes", locales), key=len, reverse=True)
    alternatives = "|".join(re.escape(p) for p in prefixes) or "(?!)"
    return re.compile(rf"(?<![^\W\d_])(?:{alternatives})\s+", re.IGNORECASE)


def _stopwords(locales) -> frozenset[str]:
    """Lo que nunca es el nombre de una persona."""
    return (label_set("greetings", locales) | label_set("closings", locales)
            | label_set("street_tokens", locales))


def _is_stopword(run: str, locales) -> bool:
    bad = _stopwords(locales)
    return any(fold(w.strip(".,;:")) in bad for w in run.split())


def _tokens(text: str, start: int) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(text, start)]


def _capitalized_runs(text: str, start: int):
    """Rachas de palabras capitalizadas separadas SOLO por espacios.

    Una coma corta la racha: 'Ana Perez, Juan Lopez' son dos nombres, no uno.
    """
    glue = name_glue_words()
    run: list[tuple[str, int, int]] = []
    pending_glue: list[tuple[str, int, int]] = []
    previous_end: int | None = None

    def flush():
        nonlocal run, pending_glue
        if run:
            yielded = (run[0][1], run[-1][2])
            run, pending_glue = [], []
            return yielded
        run, pending_glue = [], []
        return None

    for word, begin, end in _tokens(text, start):
        # entre dos palabras del nombre solo puede haber espacios
        broken = previous_end is not None and text[previous_end:begin].strip()
        previous_end = end
        if broken:
            if span := flush():
                yield span
        folded = fold(word)
        if folded in glue:
            if run and len(pending_glue) < MAX_GLUE_RUN:
                pending_glue.append((word, begin, end))
                continue
            if span := flush():
                yield span
            continue
        if word[0].isupper():
            run.extend(pending_glue)
            pending_glue = []
            run.append((word, begin, end))
            continue
        if span := flush():
            yield span
    if span := flush():
        yield span


def _acceptable(run: str, locales) -> bool:
    glue = name_glue_words()
    own = [w for w in run.split() if fold(w) not in glue]
    return (len(run) >= MIN_NAME_CHARS and 2 <= len(own) <= MAX_NAME_WORDS
            and not _is_stopword(run, locales))


def _first_field_name(text: str, locales) -> tuple[str, tuple[int, int]] | None:
    """El primer campo de un renglon con separadores, si parece un nombre.

    Pide estructura porque no tiene mayusculas de las que agarrarse: si el
    renglon no esta separado en campos, no hay candidato.
    """
    fields = list(_FIELD_SPLIT.finditer(text))
    if not fields:
        return None
    head = text[:fields[0].start()]
    stripped = head.strip()
    if not stripped:
        return None
    words = stripped.split()
    if not MIN_LOWER_WORDS <= len(words) <= MAX_LOWER_WORDS:
        return None
    if any(not word.isalpha() for word in words):
        return None
    blocked = _stopwords(locales) | label_set("deliver_linkers", locales)
    if any(fold(word) in blocked for word in words):
        return None
    if any(fold(" ".join(words)).startswith(prefix)
           for prefix in label_set("deliver_prefixes", locales)):
        return None
    begin = head.index(stripped)
    return stripped, (begin, begin + len(stripped))


def extract_customer_name(canvas, context) -> FieldValue | None:
    text = canvas.remaining()
    prefix = _prefix_pattern(context.locales).search(text)
    method, evidence = "capitalized_run", "primera racha de palabras capitalizadas"
    search_from = 0

    if prefix:
        search_from = prefix.end()
        method = "name_prefix"
        evidence = f"prefijo '{prefix.group(0).strip()}' (no se incluye en el valor)"

    for span in _capitalized_runs(text, search_from):
        run = " ".join(text[span[0]:span[1]].split())
        if not _acceptable(run, context.locales):
            continue
        confidence = CONF_PREFIX if prefix and span[0] <= search_from + 1 \
            else CONF_CAPITALIZED
        return FieldValue("customer_name", run, canvas.slice(span), confidence,
                          method, span, (evidence, f"{len(run.split())} palabras"))

    if found := _first_field_name(text, context.locales):
        run, span = found
        return FieldValue(
            "customer_name", run, canvas.slice(span), CONF_LOWERCASE,
            "first_field", span,
            ("sin mayusculas: es el primer campo del renglon, antes del separador",
             f"{len(run.split())} palabras"))
    return None
