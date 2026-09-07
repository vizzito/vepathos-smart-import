"""Telefonos con `phonenumbers` (libphonenumber), no con un regex propio.

Un regex de telefonos internacional es una trampa: confunde codigos postales,
horarios, IDs y medidas. libphonenumber ya sabe los planes de numeracion de cada
pais, valida, y devuelve el span exacto dentro del texto.

Se preserva SIEMPRE el `raw` tal cual venia; `value` es el E.164 cuando el numero
es valido para alguna region conocida.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .result import FieldValue

#: regiones a probar cuando el job no declara ninguna
FALLBACK_REGIONS = ("AR", "BR", "US", "MX", "ES", "IN", "PT", "GB")
#: minimo de digitos para siquiera considerarlo
MIN_DIGITS = 6
MAX_DIGITS = 15


@dataclass(frozen=True)
class PhoneHit:
    raw: str
    e164: str | None
    span: tuple[int, int]
    region: str
    valid: bool

    @property
    def digits(self) -> str:
        return re.sub(r"\D", "", self.raw)


def _trim(text: str, start: int, end: int) -> tuple[str, int, int]:
    """`PhoneNumberMatcher` a veces se lleva el '(' de apertura: se recorta."""
    raw = text[start:end]
    lead = 0
    while lead < len(raw) and not (raw[lead].isdigit() or raw[lead] == "+"):
        lead += 1
    trimmed = raw[lead:].rstrip(" .,;:-()[]/|")
    return trimmed, start + lead, start + lead + len(trimmed)


def find_phones(text: str, regions: tuple[str, ...] = ()) -> list[PhoneHit]:
    """Todos los telefonos del texto, en orden de aparicion, sin duplicados."""
    if not text:
        return []
    try:
        import phonenumbers
    except ImportError:                                    # pragma: no cover
        return []

    ordered = [r.upper() for r in regions if r] or []
    for extra in FALLBACK_REGIONS:
        if extra not in ordered:
            ordered.append(extra)

    hits: list[PhoneHit] = []
    seen: set[str] = set()
    for region in ordered:
        try:
            matches = list(phonenumbers.PhoneNumberMatcher(text, region))
        except Exception:
            continue
        for match in matches:
            raw, start, end = _trim(text, match.start, match.end)
            digits = re.sub(r"\D", "", raw)
            if not (MIN_DIGITS <= len(digits) <= MAX_DIGITS) or digits in seen:
                continue
            seen.add(digits)
            valid = phonenumbers.is_valid_number(match.number)
            e164 = (phonenumbers.format_number(
                match.number, phonenumbers.PhoneNumberFormat.E164) if valid else None)
            hits.append(PhoneHit(raw=raw, e164=e164, span=(start, end),
                                 region=region, valid=valid))
        if hits:
            break
    return sorted(hits, key=lambda h: h.span[0])


def extract_phone(canvas, context) -> FieldValue | None:
    """Primer telefono valido del texto restante."""
    hits = find_phones(canvas.remaining(), context.regions)
    if not hits:
        return None
    best = next((h for h in hits if h.valid), hits[0])
    evidence = [f"libphonenumber region={best.region}",
                f"{len(best.digits)} digitos"]
    if best.valid:
        evidence.append("numero valido para la region")
    return FieldValue(
        field="phone",
        value=best.e164 or best.raw,
        raw=best.raw,
        confidence=0.99 if best.valid else 0.60,
        method="phonenumbers",
        span=best.span,
        evidence=tuple(evidence),
    )
