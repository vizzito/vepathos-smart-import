"""Normalizacion de direcciones previa al geocoding.

No se descarta nada: se conserva el texto original y se agrega una version
normalizada mas los componentes que se pudieron extraer. Pensado para funcionar
tambien con direcciones de India, donde muchas veces NO hay calle + numero sino
edificio, localidad y pincode.

Abreviaturas / hints: `resources/geo_keywords.json`.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from ..resources import address_abbreviations, landmark_hints, unit_hints

# pincode (India, 6 digitos), CP argentino (4) y ZIP (5) se detectan por forma
_PINCODE_IN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_ZIP_US = re.compile(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)")
_CP_AR = re.compile(r"\b([A-Z]\d{4}[A-Z]{3})\b|(?<!\d)(\d{4})(?!\d)")
_HOUSE_NUMBER = re.compile(r"(?<!\w)(\d{1,5})(?:\s*[-/]\s*\d{1,4})?(?:[a-zA-Z](?!\w))?(?!\d)")
#: '1st' / '7th' no son altura (si no, 'NE 1st Ave 350' → house=1)
_ORDINAL = re.compile(r"^\d{1,3}(?:st|nd|rd|th)$", re.IGNORECASE)


@lru_cache(maxsize=1)
def _abbreviations() -> dict[str, str]:
    return address_abbreviations()


@lru_cache(maxsize=1)
def _unit_hints() -> tuple[str, ...]:
    return tuple(h.lower() for h in unit_hints())


@lru_cache(maxsize=1)
def _landmark_hints() -> tuple[str, ...]:
    return tuple(h.lower() for h in landmark_hints())


@dataclass
class ParsedAddress:
    original: str
    normalized: str = ""
    tokens: tuple[str, ...] = ()
    house_number: str | None = None
    #: nombre de la calle aislado ('Av. Cabildo'), cuando se pudo determinar.
    #: Sin esto el scoring compara la calle candidata contra el texto ENTERO y
    #: 'Olazabal 1728 Belgrano' matchea la calle Belgrano con 1.00.
    road: str | None = None
    postcode: str | None = None
    unit: str | None = None
    landmark: str | None = None
    parts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "original_address": self.original, "normalized_address": self.normalized,
            "house_number": self.house_number, "road": self.road,
            "postcode": self.postcode,
            "unit": self.unit, "landmark": self.landmark,
        }


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_text(text: str) -> str:
    """Minusculas, sin acentos, sin puntuacion, abreviaturas expandidas."""
    s = strip_accents(str(text)).lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    abbr = _abbreviations()
    words = [abbr.get(w, w) for w in s.split()]
    return " ".join(words)


def _extract_postcode(text: str) -> str | None:
    """Solo formas inequivocas, o un numero suelto que este al final del texto.

    Un '1234' en medio de 'Av. Corrientes 1234, CABA' es la ALTURA de la calle,
    no un codigo postal: confundirlos arruina el scoring del geocoder.
    """
    if m := _PINCODE_IN.search(text):
        return m.group(1)
    if (m := _CP_AR.search(text)) and m.group(1):
        return m.group(1)                        # forma argentina C1084ABC
    if m := _ZIP_US.search(text):
        return m.group(1)

    tail = re.split(r"[,;]", text)[-1].strip()
    if m := re.fullmatch(r"(?:[A-Za-z]{2,}\s+)?(\d{4})", tail):
        return m.group(1)
    return None


def parse(address: str) -> ParsedAddress:
    original = str(address).strip()
    if not original:
        return ParsedAddress(original="")

    parts = [p.strip() for p in re.split(r"[,;]", original) if p.strip()]
    postcode = _extract_postcode(original)

    unit = next((p for p in parts if any(h in p.lower() for h in _unit_hints())), None)
    landmark = next((p for p in parts if any(h in p.lower() for h in _landmark_hints())), None)

    parsed = {}
    try:
        parsed = _address_parser().parse(unicodedata.normalize("NFC", original))
    except Exception:
        parsed = {}
    road = (parsed.get("road") or "").strip() or None
    house_number = (parsed.get("house_number") or "").strip() or None
    if not house_number:
        for m in _HOUSE_NUMBER.finditer(original):
            candidate = m.group(1)
            token = m.group(0).strip()
            if postcode and candidate == postcode:
                continue
            if len(candidate) >= 6 or _ORDINAL.match(token):
                continue
            house_number = token
            break

    normalized = normalize_text(original)
    tokens = tuple(t for t in normalized.split() if len(t) > 1 or t.isdigit())

    return ParsedAddress(
        original=original, normalized=normalized, tokens=tokens,
        house_number=house_number, road=road,
        postcode=postcode, unit=unit, landmark=landmark,
        parts=parts,
    )


_bound_parser_config = None


def bind_parser_config(config) -> None:
    """Usa este Config en `parse()` (p.ej. geocode-accuracy con libpostal on)."""
    global _bound_parser_config
    _bound_parser_config = config
    _address_parser.cache_clear()
    from ..addresses import factory as factory_mod
    factory_mod._announced.clear()


def unbind_parser_config() -> None:
    global _bound_parser_config
    _bound_parser_config = None
    _address_parser.cache_clear()


@lru_cache(maxsize=1)
def _address_parser():
    """Mismo parser que extraction. Respeta bind_parser_config o el env."""
    from ..addresses.factory import build_address_parser
    from ..config import Config
    return build_address_parser(_bound_parser_config or Config.from_env())
