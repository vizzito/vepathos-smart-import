"""Puntaje de un candidato del indice contra la direccion buscada.

Nunca se devuelve un match debil como si fuera bueno: el score sale en la salida
y por debajo del umbral la fila queda marcada para revision, sin coordenadas
inventadas.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .address import ParsedAddress, normalize_text

# pesos: el codigo postal y la altura son las senales mas fuertes; la cercania
# al depot desempata entre calles homonimas de distintas ciudades
WEIGHTS = {
    "street": 0.55,
    "postcode": 0.18,
    "locality": 0.15,
    "name": 0.07,
    "proximity": 0.05,
}

#: La altura NO entra en el promedio ponderado sino como bonus/penalidad. Si
#: pesara como los demas, una calle acertada al 100% sin altura exacta caia por
#: debajo del umbral y se descartaba, cuando en realidad un match a nivel calle
#: es un resultado util (con precision 'street' y marcado para revision).
HOUSE_NUMBER_BONUS = 0.15
HOUSE_NUMBER_PARTIAL = 0.07
HOUSE_NUMBER_MISS_FACTOR = 0.88


@dataclass
class Candidate:
    id: int
    lat: float
    lon: float
    kind: str | None
    name: str | None
    house_number: str | None
    street: str | None
    city: str | None
    district: str | None
    state: str | None
    postcode: str | None
    country: str | None
    normalized_text: str

    @property
    def precision(self) -> str:
        if self.house_number and self.street:
            return "housenumber"
        if self.street:
            return "street"
        if self.name:
            return "poi"
        return "locality"


MIN_SUBSTRING = 4          # 'st', 'av' o 'the' adentro del texto no son evidencia


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    from rapidfuzz import fuzz
    return max(fuzz.token_sort_ratio(a, b), fuzz.ratio(a, b)) / 100.0


def _contains(haystack: str, needle: str) -> bool:
    return len(needle) >= MIN_SUBSTRING and needle in haystack


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def score(parsed: ParsedAddress, cand: Candidate,
          origin: tuple[float, float] | None = None) -> tuple[float, dict]:
    parts: dict[str, float] = {}
    text = parsed.normalized

    cand_street = normalize_text(cand.street or "")
    cand_name = normalize_text(cand.name or "")

    # calle: se compara contra el texto completo porque la direccion del cliente
    # trae la calle mezclada con ciudad y referencias
    parts["street"] = max(_similarity(text, cand_street),
                          1.0 if _contains(text, cand_street) else 0.0)

    if parsed.postcode and cand.postcode:
        parts["postcode"] = 1.0 if parsed.postcode.strip() == cand.postcode.strip() else 0.0
    else:
        parts["postcode"] = 0.0

    locality = " ".join(normalize_text(p) for p in (cand.city, cand.district, cand.state) if p)
    parts["locality"] = 1.0 if locality and any(tok in text for tok in locality.split()) else 0.0

    parts["name"] = max(_similarity(text, cand_name),
                        1.0 if _contains(text, cand_name) else 0.0)

    if origin:
        km = haversine_km(origin[0], origin[1], cand.lat, cand.lon)
        # 0 km -> 1.0 ; 50 km -> ~0.0. Es un desempate, no un criterio principal.
        parts["proximity"] = max(0.0, 1.0 - km / 50.0)
    else:
        parts["proximity"] = 0.0

    # el codigo postal solo cuenta si la consulta lo traia
    applicable = {k: w for k, w in WEIGHTS.items()
                  if not (k == "postcode" and not parsed.postcode)
                  and not (k == "proximity" and not origin)}
    total_weight = sum(applicable.values()) or 1.0
    value = sum(parts[k] * w for k, w in applicable.items()) / total_weight

    house = _house_number_match(parsed, cand)
    parts["house_number"] = house
    if parsed.house_number:
        if house >= 1.0:
            value = min(1.0, value + HOUSE_NUMBER_BONUS)
        elif house > 0.0:
            value = min(1.0, value + HOUSE_NUMBER_PARTIAL)
        else:
            value *= HOUSE_NUMBER_MISS_FACTOR

    return round(value, 4), {k: round(v, 3) for k, v in parts.items()}


def _house_number_match(parsed: ParsedAddress, cand: Candidate) -> float:
    if not (parsed.house_number and cand.house_number):
        return 0.0
    want = parsed.house_number.strip().lower()
    got = cand.house_number.strip().lower()
    if want == got:
        return 1.0
    strip = "abcdefghijklmnopqrstuvwxyz"
    return 0.5 if want.rstrip(strip) == got.rstrip(strip) else 0.0
