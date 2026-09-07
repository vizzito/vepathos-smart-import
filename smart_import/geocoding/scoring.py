"""Puntaje de un candidato del indice contra la direccion buscada.

Nunca se devuelve un match debil como si fuera bueno: el score sale en la salida
y por debajo del umbral la fila queda marcada para revision, sin coordenadas
inventadas.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache

from ..resources import fold
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

#: NE/NW/1st: la grilla US. 'northeast 71st' no puede parecerse 0.80 a 'NE 1st'.
_ORDINAL_RE = re.compile(r"^(\d{1,3})(?:st|nd|rd|th)$", re.IGNORECASE)
_ORDINAL_WORDS = {
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "sixth": "6", "seventh": "7", "eighth": "8", "ninth": "9", "tenth": "10",
}
_COMPASS_KEY = {
    "ne": "ne", "northeast": "ne", "noreste": "ne",
    "nw": "nw", "northwest": "nw", "noroeste": "nw",
    "se": "se", "southeast": "se", "sureste": "se",
    "sw": "sw", "southwest": "sw", "suroeste": "sw",
}
_YEAR_RE = re.compile(r"^\d{4}$")


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    from rapidfuzz import fuzz
    return max(fuzz.token_sort_ratio(a, b), fuzz.ratio(a, b)) / 100.0


def _contains(haystack: str, needle: str) -> bool:
    return len(needle) >= MIN_SUBSTRING and needle in haystack


def compass_key(token: str) -> str | None:
    return _COMPASS_KEY.get(token.casefold())


def ordinal_key(token: str) -> str | None:
    folded = token.casefold()
    if folded in _ORDINAL_WORDS:
        return _ORDINAL_WORDS[folded]
    match = _ORDINAL_RE.fullmatch(folded)
    if not match:
        return None
    return match.group(1).lstrip("0") or "0"


def _fold_grid_tokens(nombre: str) -> str:
    """'noreste 1st' y 'northeast first' → 'ne 1st' para comparar igual."""
    out: list[str] = []
    for word in nombre.split():
        if key := compass_key(word):
            out.append(key)
            continue
        if key := ordinal_key(word):
            out.append(f"{key}st")
            continue
        out.append(word)
    return " ".join(out)


def _token_sets(nombre: str) -> tuple[set[str], set[str]]:
    compass, ordinals = set(), set()
    for word in nombre.split():
        if key := compass_key(word):
            compass.add(key)
        if key := ordinal_key(word):
            ordinals.add(key)
    return compass, ordinals


def _street_name_tokens(nombre: str) -> set[str]:
    """Tokens que distinguen una calle. Glue, tipo de via y años no cuentan."""
    from ..resources import fold, label_set, name_glue_words

    skip = name_glue_words() | label_set("street_tokens", None) | {
        "de", "of", "the", "la", "el", "del", "los", "las", "y", "and",
    }
    return {w for w in nombre.split()
            if w and fold(w) not in skip and not _YEAR_RE.fullmatch(w)}


def _candidate_extras(query_name: str, cand_name: str) -> set[str]:
    """Tokens del candidato que no estan en la query (Brickell Key, Cabo Corrientes)."""
    return _street_name_tokens(cand_name) - _street_name_tokens(query_name)


@lru_cache(maxsize=1)
def _locality_vocab() -> tuple[frozenset[str], frozenset[str]]:
    """(cues de una palabra, frases). GeoNames + aliases curados."""
    from ..resources import locality_alias_groups, locality_expansions

    singles: set[str] = set()
    phrases: set[str] = set()
    for group in locality_alias_groups():
        for alias in group:
            if " " in alias:
                phrases.add(alias)
            elif alias:
                singles.add(alias)
    for cues, _tokens in locality_expansions():
        for cue in cues:
            if " " in cue:
                phrases.add(cue)
            elif cue:
                singles.add(cue)
    return frozenset(singles), frozenset(phrases)


def _locality_tokens_in(nombre: str) -> set[str]:
    """Tokens del nombre que son ciudad/barrio/pais, no parte de la calle.

    'collins miami beach': miami es cue; miami beach es frase GeoNames.
    'fragata sarmiento': ninguno — Fragata es el nombre de la calle.
    """
    singles, phrases = _locality_vocab()
    words = nombre.split()
    covered: set[str] = {w for w in words if w in singles}
    folded = f" {nombre} "
    for phrase in phrases:
        if f" {phrase} " in folded:
            covered.update(phrase.split())
    return covered


def _query_street_leftovers(query_name: str, cand_name: str) -> set[str]:
    """Tokens de la calle pedida que el candidato no tiene.

    Fragata Sarmiento ⊃ Sarmiento deja 'fragata' → otra calle.
    Collins Ave Miami Beach ⊃ Collins deja 'miami'+'beach' → localidad, se ignora.
    """
    extras = _street_name_tokens(query_name) - _street_name_tokens(cand_name)
    return extras - _locality_tokens_in(query_name)


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

    parts["street"] = _street_score(parsed, cand_street, text)

    if parsed.postcode and cand.postcode:
        parts["postcode"] = 1.0 if parsed.postcode.strip() == cand.postcode.strip() else 0.0
    else:
        parts["postcode"] = 0.0

    parts["locality"] = _locality_score(text, cand)

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


def _strip_way_type(nombre: str) -> str:
    """Saca el tipo de via generico: 'avenida cabildo' -> 'cabildo'.

    Sin esto, 'Avenida Las Heras' y 'Avenida Caseros' se parecen 0.81 porque el
    token 'avenida' domina el fuzzy — y el geocoder elige la calle equivocada a
    5 km desempatando por cercania al depot. Comparar los NOMBRES es lo que
    distingue 'Las Heras' de 'Caseros'.
    """
    from ..resources import label_set

    tokens = label_set("street_tokens", None)
    palabras = [w for w in nombre.split() if w and fold(w.strip(".,;:")) not in tokens]
    # Si la calle SE LLAMA como el tipo ('Avenida de Mayo'), no dejarla vacia.
    return " ".join(palabras) if palabras else nombre


def _street_score(parsed: ParsedAddress, cand_street: str, text: str) -> float:
    """Calle contra calle cuando se pudo aislar; contra el texto entero si no.

    Comparar la calle candidata contra la frase completa produce falsos positivos
    caros: en 'Olazabal 1728 Belgrano' el barrio 'Belgrano' matchea la CALLE
    Belgrano con 1.00 y el pin termina a 5 km. Cuando el parser aisla la calle
    ('Olazabal'), se compara contra eso.

    Sin calle aislada —POIs, direcciones de India sin calle+altura— se conserva
    el comportamiento anterior: ahi el texto completo es la mejor evidencia que hay.
    """
    if not cand_street:
        return 0.0
    if parsed.road:
        road = _fold_grid_tokens(normalize_text(parsed.road))
        cand_street = _fold_grid_tokens(cand_street)
        nombre_a, nombre_b = _strip_way_type(road), _strip_way_type(cand_street)
        compass_a, ord_a = _token_sets(nombre_a)
        compass_b, ord_b = _token_sets(nombre_b)
        # Grilla US: NE ≠ NW y 1st ≠ 71st. Fuzzy 0.80 acá es un pin a 7 km.
        if compass_a and compass_b and compass_a.isdisjoint(compass_b):
            return 0.0
        if ord_a and ord_b and ord_a.isdisjoint(ord_b):
            return 0.0
        # El nombre pedido manda: 'Fragata Sarmiento' no es 'Sarmiento'
        # aunque una sea substring de la otra. Localidad pegada al road
        # (Miami Beach) no cuenta como token de calle. Ave≠Street no: son tipo.
        if _query_street_leftovers(nombre_a, nombre_b):
            return 0.0
        if _contains(nombre_a, nombre_b) or _contains(nombre_b, nombre_a):
            if not _candidate_extras(nombre_a, nombre_b):
                return 1.0
        if _contains(road, cand_street) or _contains(cand_street, road):
            if not _candidate_extras(road, cand_street):
                return 1.0
        return _similarity(nombre_a, nombre_b)
    return max(_similarity(text, cand_street),
               1.0 if _contains(text, cand_street) else 0.0)


def _house_key(raw: str | None) -> str:
    """'03845', '3845A' → '3845' para comparar altura, no el string crudo."""
    if not raw:
        return ""
    token = raw.strip().lower()
    letters = "abcdefghijklmnopqrstuvwxyz"
    core = token.rstrip(letters)
    digits = "".join(ch for ch in core if ch.isdigit())
    return digits.lstrip("0") or (digits if digits else token)


def _house_int(raw: str | None) -> int | None:
    key = _house_key(raw)
    if key.isdigit():
        return int(key)
    return None


def _locality_labels(text: str) -> set[str]:
    """Tokens + aliases que aparecen como frase ('caba', 'capital federal')."""
    from ..resources import locality_alias_groups

    folded = fold(text)
    labels = {folded}
    labels.update(fold(tok) for tok in text.split() if len(tok) >= 3)
    for group in locality_alias_groups():
        for alias in group:
            if len(alias) >= 4 and alias in folded:
                labels.add(alias)
    return labels


def _expand_locality(labels: set[str]) -> set[str]:
    from ..resources import locality_alias_groups

    out = set(labels)
    for group in locality_alias_groups():
        if labels & group:
            out |= set(group)
    return out


def _locality_score(text: str, cand: Candidate) -> float:
    """CABA en la query tiene que matchear 'Ciudad Autónoma de Buenos Aires'.

    No se iguala CABA con la provincia: son grupos distintos en locality_expand.
    """
    cand_labels = {
        fold(normalize_text(p))
        for p in (cand.city, cand.district, cand.state) if p
    }
    if not cand_labels:
        return 0.0
    skip = {"de", "la", "el", "del", "los", "las", "the", "and", "city"}
    query = _expand_locality(_locality_labels(text)) - skip
    city = _expand_locality(cand_labels) - skip
    return 1.0 if query & city else 0.0


def _house_number_match(parsed: ParsedAddress, cand: Candidate) -> float:
    if not (parsed.house_number and cand.house_number):
        return 0.0
    want = parsed.house_number.strip().lower()
    got = cand.house_number.strip().lower()
    if want == got or _house_key(want) == _house_key(got):
        return 1.0
    strip = "abcdefghijklmnopqrstuvwxyz"
    if want.rstrip(strip) == got.rstrip(strip):
        return 0.5
    wi, gi = _house_int(want), _house_int(got)
    if wi is None or gi is None:
        return 0.0
    delta = abs(wi - gi)
    if delta <= 20:
        return 0.55
    if delta <= 80:
        return 0.25
    return 0.0
