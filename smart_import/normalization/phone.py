"""Normalizacion de telefonos. Si hay duda, se PRESERVA el valor original."""
from __future__ import annotations

import re

#: 'Mobile: 1199887766' / 'Tel- 4000 1000': la etiqueta viaja dentro de la celda.
#: Solo se saca cuando el valor NO empieza con digito, '+' ni '(' — asi un
#: telefono normal ('11-4000-1000', '(011) 4000-1000') nunca se toca.
_CELL_LABEL = re.compile(r"^[^\d+(]{1,24}[:\-]\s*")


def _strip_label(text: str) -> str:
    if text[:1].isdigit() or text[:1] in "+(":
        return text
    return _CELL_LABEL.sub("", text, count=1).strip()


def normalize(raw: str | None, region: str | None = None) -> tuple[str | None, str | None]:
    """(valor_normalizado, warning). Nunca descarta el original."""
    if not raw:
        return None, None
    text = _strip_label(str(raw).strip())
    if not text:
        return None, None
    if not region:
        return text, None
    try:
        import phonenumbers
    except ImportError:
        return text, None
    try:
        parsed = phonenumbers.parse(text, region)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164), None
    except Exception:
        pass
    return text, None                       # invalido para la region: se deja como vino
