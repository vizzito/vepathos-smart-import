"""Cuanta evidencia hay de que un texto sea una direccion.

Existe para que "Salutos" nunca llegue al geocoder y para que "Belgrano" —una
localidad— no se confunda con una direccion precisa. Devuelve el score Y las
razones: sin las razones no se pueden mejorar las reglas despues.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..resources import fold, label_set
from .base import ParsedAddress

# 'Corrientes 100' / '23 MG Road' / 'Flat 14B' — numero pegado a palabras
_NUMBER_NEAR_WORD = re.compile(
    r"(?:(?<![^\W\d_])[^\W\d_]{2,}[\s.,]+\d{1,5}[A-Za-z]?(?![\d/])"
    r"|(?<!\d)\d{1,5}[A-Za-z]?\s+[^\W\d_]{2,})", re.UNICODE)
# CPA argentino (C1425DKE), pincode indio (400069) o CP de 4 digitos al final.
# Un 4 digitos en el medio es la altura de la calle, no un codigo postal.
_POSTCODE = re.compile(r"(?<![\w])(?:[A-Z]\d{4}[A-Z]{3}|\d{5,6}(?![\w])|\d{4}\s*$)")
# cola de segmentos capitalizados: ', Palermo, CABA' / ', Andheri East, Mumbai'
_LOCALITY_TAIL = re.compile(r",\s*[^\W\d_][^,]{2,40}(?:,|$)", re.UNICODE)

#: pesos de cada señal. Suman mas de 1: el score se recorta arriba.
W_HOUSE_NUMBER = 0.45
W_STREET_TOKEN = 0.25
W_POSTCODE = 0.20
W_LOCALITY = 0.15
W_LENGTH = 0.10
W_PRECISE_COMPONENT = 0.30

MIN_WORDS_FOR_LENGTH = 3


@dataclass(frozen=True)
class AddressScore:
    score: float
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {"score": round(self.score, 3), "evidence": list(self.evidence)}


class AddressCandidateScorer:
    """Puntua texto crudo o una `ParsedAddress`. Sin gazetteer, sin pais fijo."""

    def __init__(self, locales: tuple[str, ...] | None = None):
        self.locales = locales

    def _street_tokens(self) -> frozenset[str]:
        return label_set("street_tokens", self.locales)

    def score(self, candidate: str | ParsedAddress) -> AddressScore:
        parsed = candidate if isinstance(candidate, ParsedAddress) else None
        text = parsed.text if parsed else str(candidate or "")
        stripped = text.strip()
        if len(stripped) < 4:
            return AddressScore(0.0, ("texto demasiado corto para ser una direccion",))

        total = 0.0
        why: list[str] = []
        words = stripped.split()

        if _NUMBER_NEAR_WORD.search(stripped):
            total += W_HOUSE_NUMBER
            why.append("numero de puerta junto a un nombre de calle")

        tokens = self._street_tokens()
        hit = next((w for w in words if fold(w.strip(".,;:")) in tokens), None)
        if hit:
            total += W_STREET_TOKEN
            why.append(f"token de via '{hit}'")

        if postcode := _POSTCODE.search(stripped):
            total += W_POSTCODE
            why.append(f"codigo postal '{postcode.group(0)}'")

        if _LOCALITY_TAIL.search(stripped):
            total += W_LOCALITY
            why.append("cola de localidad/barrio separada por comas")

        if len(words) >= MIN_WORDS_FOR_LENGTH:
            total += W_LENGTH
            why.append(f"{len(words)} palabras")

        if parsed is not None and parsed.is_precise:
            total += W_PRECISE_COMPONENT
            present = [c for c in ("house_number", "road", "unit", "building", "postcode")
                       if parsed.get(c)]
            why.append(f"{parsed.parser} identifico {'+'.join(present)}")

        if not why:
            why.append("sin ninguna señal de direccion")
        return AddressScore(min(round(total, 3), 0.99), tuple(why))

    def is_geocodable(self, candidate: str | ParsedAddress, threshold: float) -> bool:
        """Gate previo al geocoder: nunca mandar basura a resolver."""
        return self.score(candidate).score >= threshold
