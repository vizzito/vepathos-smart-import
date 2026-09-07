"""Notas operativas: lo que sobra despues de identificar todo lo demas.

Se corre ultimo a proposito. Lo que queda sin consumir y tiene cuerpo suficiente
es una instruccion para el repartidor ("tocar timbre 4A", "si no contesta llamar").
Nunca se inventa: si no sobro nada, no hay reference.
"""
from __future__ import annotations

import re

from .labels import tidy
from .result import FieldValue

_EMAIL = re.compile(r"(?<![\w.])[\w.+-]+@[\w-]+\.[\w.-]{2,}(?![\w])")
_CLAUSE = re.compile(r"[.;|!?\n]|//|--")

#: menos que esto es ruido de puntuacion, no una nota
MIN_REFERENCE_WORDS = 4
MAX_REFERENCE_CHARS = 240


def extract_email(canvas, context) -> FieldValue | None:
    match = _EMAIL.search(canvas.remaining())
    if not match:
        return None
    return FieldValue("email", match.group(0), match.group(0), 0.97, "email_regex",
                      match.span(), ("direccion de correo",))


def extract_reference(canvas, context) -> FieldValue | None:
    """Clausulas ENTERAS que ningun extractor toco.

    El filtro clave es que la clausula este intacta. Si adentro hubo una direccion
    o un telefono, lo que sobra es la narracion que los envolvia —"paso a avisar
    que vive en ... y el cel de ella es ..."— y eso no es una instruccion para el
    repartidor: es ruido. Una nota de verdad viene sola.
    """
    text = canvas.original
    parts: list[tuple[int, str]] = []
    position = 0
    for match in [*_CLAUSE.finditer(text), None]:
        end = match.start() if match else len(text)
        chunk = tidy(text[position:end])
        if len(chunk.split()) >= MIN_REFERENCE_WORDS and canvas.is_free((position, end)):
            parts.append((position, chunk))
        position = (match.end() if match else len(text))

    if not parts:
        return None
    value = "; ".join(chunk for _, chunk in parts)[:MAX_REFERENCE_CHARS]
    start = parts[0][0]
    return FieldValue("reference", value, value, 0.55, "leftover_clause",
                      (start, start + len(parts[0][1])),
                      (f"{len(parts)} clausula(s) intacta(s) sin consumir",))
