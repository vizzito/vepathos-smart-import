"""Normalizacion de telefonos. Si hay duda, se PRESERVA el valor original."""
from __future__ import annotations


def normalize(raw: str | None, region: str | None = None) -> tuple[str | None, str | None]:
    """(valor_normalizado, warning). Nunca descarta el original."""
    if not raw:
        return None, None
    text = str(raw).strip()
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
