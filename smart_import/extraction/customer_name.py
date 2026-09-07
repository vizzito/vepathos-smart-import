"""El nombre del cliente, DESPUES de resolver telefono, direccion y etiquetas.

El nombre es el campo menos objetivo: se resuelve ultimo, cuando lo demas ya
salio del texto y queda mucha menos ambiguedad. Dos reglas, en orden:

  1. hay un prefijo explicito -> lo que sigue     'Para Maria Gomez'   -> Maria Gomez
  2. no lo hay -> la primera racha de palabras capitalizadas del resto

El prefijo NUNCA queda adentro del valor: 'Para', 'Destinatario:' y 'CONTACTO:'
son etiquetas, no parte del nombre.
"""
from __future__ import annotations

import re

from functools import lru_cache

from ..resources import fold, label_set, name_glue_words
from .result import FieldValue

@lru_cache(maxsize=1)
def _caps_run() -> re.Pattern[str]:
    """Una racha de 2 a 4 palabras capitalizadas, con conectores de nombre en medio.

    Los conectores ('de', 'van', 'bin', 'der') salen de `resources`: son la MISMA
    lista que usa el parser de direcciones. Duplicarlos en un regex es como se
    desincronizan dos modulos que creen tener la misma regla.
    """
    glue = "|".join(re.escape(w) for w in
                    sorted(name_glue_words(), key=len, reverse=True))
    palabra = r"[A-ZÁÉÍÓÚÜÑ][^\W\d_]{1,20}"
    # Hasta DOS conectores seguidos, y en cualquier posicion: 'Maria de los
    # Angeles Perez', 'Jan van der Berg'. Con uno solo se cortaban los dos.
    return re.compile(
        rf"(?<![^\W\d_])(?P<run>{palabra}"
        rf"(?:\s+(?:(?:{glue})\s+){{0,2}}{palabra}){{1,3}})", re.UNICODE)

MIN_NAME_CHARS = 4
MAX_NAME_WORDS = 4


def _prefix_pattern(locales: tuple[str, ...] | None) -> re.Pattern[str]:
    prefixes = sorted(label_set("name_prefixes", locales), key=len, reverse=True)
    alternatives = "|".join(re.escape(p) for p in prefixes) or "(?!)"
    return re.compile(rf"(?<![^\W\d_])(?:{alternatives})\s+", re.IGNORECASE)


def _is_stopword(run: str, locales) -> bool:
    """Saludos, despedidas y tokens de via no son nombres de persona."""
    bad = (label_set("greetings", locales) | label_set("closings", locales)
           | label_set("street_tokens", locales))
    words = [fold(w.strip(".,;:")) for w in run.split()]
    return any(w in bad for w in words)


def extract_customer_name(canvas, context) -> FieldValue | None:
    text = canvas.remaining()
    prefix = _prefix_pattern(context.locales).search(text)
    method, evidence = "capitalized_run", "primera racha de palabras capitalizadas"
    search_from = 0

    if prefix:
        search_from = prefix.end()
        method = "name_prefix"
        evidence = f"prefijo '{prefix.group(0).strip()}' (no se incluye en el valor)"

    for match in _caps_run().finditer(text, search_from):
        run = " ".join(match.group("run").split())
        # Los conectores no cuentan contra el tope: 'Maria de los Angeles Perez'
        # son tres palabras de nombre, no cinco.
        propias = [w for w in run.split() if fold(w) not in name_glue_words()]
        if len(run) < MIN_NAME_CHARS or len(propias) > MAX_NAME_WORDS:
            continue
        if _is_stopword(run, context.locales):
            continue
        span = match.span("run")
        confidence = 0.90 if prefix and match.start() <= search_from + 1 else 0.75
        return FieldValue("customer_name", run, canvas.slice(span), confidence,
                          method, span, (evidence, f"{len(run.split())} palabras"))
    return None
