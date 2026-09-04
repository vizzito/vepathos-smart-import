"""Normalizacion de direcciones previa al geocoding.

No se descarta nada: se conserva el texto original y se agrega una version
normalizada mas los componentes que se pudieron extraer. Pensado para funcionar
tambien con direcciones de India, donde muchas veces NO hay calle + numero sino
edificio, localidad y pincode.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# abreviatura -> forma larga. Configurable: agregar una fila no toca el codigo.
ABBREVIATIONS = {
    "av": "avenida", "avda": "avenida", "ave": "avenue", "avd": "avenida",
    "c": "calle", "cl": "calle", "st": "street", "str": "street",
    "rd": "road", "dr": "drive", "blvd": "boulevard", "bvd": "boulevard",
    "ln": "lane", "ct": "court", "pl": "place", "sq": "square", "hwy": "highway",
    "pje": "pasaje", "psje": "pasaje", "dpto": "departamento", "depto": "departamento",
    "dto": "departamento", "piso": "piso", "pb": "planta baja",
    "nte": "norte", "sur": "sur", "ote": "oeste", "n": "norte", "s": "sur",
    "e": "este", "w": "oeste", "ne": "noreste", "nw": "noroeste",
    "flt": "flat", "apt": "apartment", "bldg": "building", "opp": "opposite",
    "nr": "near", "sec": "sector", "ph": "phase", "extn": "extension",
}

# pincode (India, 6 digitos), CP argentino (4) y ZIP (5) se detectan por forma
_PINCODE_IN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_ZIP_US = re.compile(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)")
_CP_AR = re.compile(r"\b([A-Z]\d{4}[A-Z]{3})\b|(?<!\d)(\d{4})(?!\d)")
_HOUSE_NUMBER = re.compile(r"(?<!\w)(\d{1,5})(?:\s*[-/]\s*\d{1,4})?(?:[a-zA-Z](?!\w))?(?!\d)")

_LANDMARK_HINTS = ("near", "opposite", "opp", "behind", "next to", "cerca de", "frente a",
                   "al lado de", "landmark", "junto a")
_UNIT_HINTS = ("flat", "apt", "apartment", "piso", "depto", "departamento", "dpto",
               "unit", "suite", "block", "tower", "torre", "local", "oficina", "office")


@dataclass
class ParsedAddress:
    original: str
    normalized: str = ""
    tokens: tuple[str, ...] = ()
    house_number: str | None = None
    postcode: str | None = None
    unit: str | None = None
    landmark: str | None = None
    parts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "original_address": self.original, "normalized_address": self.normalized,
            "house_number": self.house_number, "postcode": self.postcode,
            "unit": self.unit, "landmark": self.landmark,
        }


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_text(text: str) -> str:
    """Minusculas, sin acentos, sin puntuacion, abreviaturas expandidas."""
    s = strip_accents(str(text)).lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    words = [ABBREVIATIONS.get(w, w) for w in s.split()]
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

    lower = original.lower()
    unit = next((p for p in parts if any(h in p.lower() for h in _UNIT_HINTS)), None)
    landmark = next((p for p in parts if any(h in p.lower() for h in _LANDMARK_HINTS)), None)

    # numero de puerta: el primer numero que NO sea el codigo postal
    house_number = None
    for m in _HOUSE_NUMBER.finditer(original):
        candidate = m.group(1)
        if postcode and candidate == postcode:
            continue
        if len(candidate) >= 6:
            continue
        house_number = m.group(0).strip()
        break

    normalized = normalize_text(original)
    tokens = tuple(t for t in normalized.split() if len(t) > 1 or t.isdigit())

    return ParsedAddress(
        original=original, normalized=normalized, tokens=tokens,
        house_number=house_number, postcode=postcode, unit=unit, landmark=landmark,
        parts=parts,
    )
