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
#: Calle+altura en OTRA ciudad/CP no puede pintar verde (~0.73 hn).
#: Por debajo de review_band (0.70): sin pin, no un homónimo a 15 km.
PLACE_MISMATCH_CAP = 0.45
#: Comuna vs capital del extract (Maipú/Santiago) queda adentro; Radom vs
#: Varsovia o Holstebro vs Copenhague no. Igual al clamp del extract (~80 km)
#: sería demasiado: San Martín Hidalgo (~60 km) seguiría pintando Guadalajara.
PLACE_FAR_KM = 50.0
_PLACE_PREFIX = re.compile(
    r"^(?:former|old|historic|city of|ciudad de|comuna(?: de)?|"
    r"commune de|municipio de|municipalidad de)\s+",
    re.IGNORECASE,
)


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
    "e": "e", "east": "e", "este": "e",
    "w": "w", "west": "w", "oeste": "w",
    "n": "n", "north": "n", "norte": "n",
    "s": "s", "south": "s", "sur": "s",
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


def en_ordinal(n: str) -> str:
    """'55' → '55th', '1' → '1st'. Lo que OSM US indexa, no el canónico 55st."""
    digits = (n or "").lstrip("0") or "0"
    if not digits.isdigit():
        return n
    value = int(digits)
    mod100, mod10 = value % 100, value % 10
    if mod100 in (11, 12, 13):
        suffix = "th"
    elif mod10 == 1:
        suffix = "st"
    elif mod10 == 2:
        suffix = "nd"
    elif mod10 == 3:
        suffix = "rd"
    else:
        suffix = "th"
    return f"{digits}{suffix}"


def ordinal_key(token: str) -> str | None:
    folded = token.casefold()
    if folded in _ORDINAL_WORDS:
        return _ORDINAL_WORDS[folded]
    match = _ORDINAL_RE.fullmatch(folded)
    if match:
        return match.group(1).lstrip("0") or "0"
    # Grilla US: 'E 55 ST' vs OSM 'East 55th Street'. 1-3 dígitos son el nombre.
    if folded.isdigit() and 1 <= len(folded) <= 3:
        return folded.lstrip("0") or "0"
    return None


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


def _postcode_keys(raw: str | None) -> set[str]:
    """'C1425', '1425', '10309-1234', '75006' → claves comparables."""
    if not raw or not str(raw).strip():
        return set()
    compact = "".join(ch for ch in fold(str(raw)) if ch.isalnum())
    if not compact:
        return set()
    keys = {compact}
    digits = "".join(ch for ch in compact if ch.isdigit())
    if len(digits) >= 4:
        keys.add(digits)
        keys.add(digits[:5] if len(digits) >= 5 else digits)
    return keys


def _postcodes_compatible(a: str | None, b: str | None) -> bool:
    return bool(_postcode_keys(a) & _postcode_keys(b))


def score(parsed: ParsedAddress, cand: Candidate,
          origin: tuple[float, float] | None = None,
          aliases: frozenset[str] | None = None) -> tuple[float, dict]:
    parts: dict[str, float] = {}
    text = parsed.normalized

    cand_street = normalize_text(cand.street or "")
    cand_name = normalize_text(cand.name or "")

    parts["street"] = _street_score(parsed, cand_street, text, aliases=aliases)
    if parts["street"] < 1.0 and cand_name and cand_name != cand_street:
        parts["street"] = max(
            parts["street"],
            _street_score(parsed, cand_name, text, aliases=aliases),
        )

    if parsed.postcode and cand.postcode:
        parts["postcode"] = 1.0 if _postcodes_compatible(parsed.postcode, cand.postcode) else 0.0
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

    mismatch = place_conflict(parsed, cand)
    parts["place_mismatch"] = 1.0 if mismatch else 0.0
    if mismatch:
        value = min(value, PLACE_MISMATCH_CAP)

    return round(value, 4), {k: round(v, 3) for k, v in parts.items()}


def _strip_way_type(nombre: str) -> str:
    """Saca el tipo de via generico: 'avenida cabildo' -> 'cabildo'.

    Sin esto, 'Avenida Las Heras' y 'Avenida Caseros' se parecen 0.81 porque el
    token 'avenida' domina el fuzzy — y el geocoder elige la calle equivocada a
    5 km desempatando por cercania al depot. Comparar los NOMBRES es lo que
    distingue 'Las Heras' de 'Caseros'.

    En neerlandes/aleman el tipo va PEGADO: 'Mozartstraat' -> 'mozart', igual
    que 'Avenue Mozart' -> 'mozart'. Los sufijos se prueban del mas largo al
    mas corto para no cortar 'straat' como si fuera 'str'.
    """
    from ..resources import label_set

    tokens = label_set("street_tokens", None)
    suffixes = sorted(label_set("street_suffixes", None), key=len, reverse=True)
    palabras: list[str] = []
    for w in nombre.split():
        if not w:
            continue
        folded = fold(w.strip(".,;:"))
        if folded in tokens:
            continue
        stem = folded
        for suf in suffixes:
            if len(stem) > len(suf) + 2 and stem.endswith(suf):
                cut = stem[:-len(suf)]
                if len(cut) >= 3:
                    stem = cut
                    break
        palabras.append(stem)
    return " ".join(palabras) if palabras else nombre


def is_name_initial(words: list[str], index: int) -> bool:
    """True si la letra suelta es la inicial de un nombre: 'juan B justo'.

    Tiene que estar ENTRE dos palabras. Una letra pegada a un numero es parte de
    una via numerada ('16 Avenida B 0-26' en Guatemala, '95 C Este' en Panama) y
    una al final es un ordinal romano ('Tebet Utara I'): tratarlas como inicial
    las emparejaba con cualquier palabra que empiece con esa letra.
    """
    if not 0 < index < len(words) - 1:
        return False
    word, before, after = words[index], words[index - 1], words[index + 1]
    return (len(word) == 1 and word.isalpha()
            and len(before) >= 3 and before.isalpha() and after.isalpha())


def _align_initials(query_name: str, cand_name: str) -> str:
    """El nombre pedido con sus iniciales completadas, si es la misma calle.

    'juan b justo' y 'juan bautista justo' (asi guarda OSM las alturas de CABA)
    tienen las mismas palabras salvo una inicial: son la misma via. Sin esto la
    'b' quedaba como token de la calle pedida que el candidato no tiene y la
    calle puntuaba 0. Se exige el mismo largo, que TODA otra palabra coincida
    ('juan b justo' no se alinea con 'juan bautista alberdi') y que la letra sea
    una inicial de verdad (`is_name_initial`).
    """
    a, b = query_name.split(), cand_name.split()
    if len(a) != len(b) or a == b:
        return query_name
    initials = 0
    for index, (x, y) in enumerate(zip(a, b)):
        if x == y:
            continue
        short, full, words = (x, y, a) if len(x) < len(y) else (y, x, b)
        if (is_name_initial(words, index) and len(full) >= 3
                and full.startswith(short)):
            initials += 1
            continue
        return query_name
    return cand_name if initials else query_name


def _street_score(parsed: ParsedAddress, cand_street: str, text: str,
                  aliases: frozenset[str] | None = None) -> float:
    """Calle contra calle cuando se pudo aislar; contra el texto entero si no.

    Comparar la calle candidata contra la frase completa produce falsos positivos
    caros: en 'Olazabal 1728 Belgrano' el barrio 'Belgrano' matchea la CALLE
    Belgrano con 1.00 y el pin termina a 5 km. Cuando el parser aisla la calle
    ('Olazabal'), se compara contra eso.

    Sin calle aislada —POIs, direcciones de India sin calle+altura— se conserva
    el comportamiento anterior: ahi el texto completo es la mejor evidencia que hay.

    `aliases` son grafias OSM de la MISMA via (name:fr ↔ name:nl). Sin esto
    'Avenue Mozart' vs 'Mozartstraat' pierde en fuzzy aunque el barrido las
    haya emparejado.
    """
    if not cand_street:
        return 0.0
    if aliases and normalize_text(cand_street) in aliases:
        return 1.0
    if parsed.road:
        road = _fold_grid_tokens(normalize_text(parsed.road))
        cand_street = _fold_grid_tokens(cand_street)
        nombre_a, nombre_b = _strip_way_type(road), _strip_way_type(cand_street)
        nombre_a = _align_initials(nombre_a, nombre_b)
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


_HOUSE_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _house_token(raw: str | None) -> str:
    """'15/B' y '15B' son la misma altura; el slash es puntuación de interno."""
    return (raw or "").strip().lower().replace("/", "")


def _house_key(raw: str | None) -> str:
    """'03845', '3845A', '15/B' → '3845' / '15' para comparar altura."""
    token = _house_token(raw)
    if not token:
        return ""
    core = token.rstrip(_HOUSE_LETTERS)
    digits = "".join(ch for ch in core if ch.isdigit())
    return digits.lstrip("0") or (digits if digits else token)


def _house_suffix(raw: str | None) -> str:
    """'4T' / '4ter' / '3845A' / '15/B' → letras; '108-15' → ''."""
    token = _house_token(raw)
    if not token:
        return ""
    core = token.rstrip(_HOUSE_LETTERS)
    return token[len(core):]


def _has_letter_intern(raw: str | None) -> bool:
    """'15/B' / '33/A': interno slash+letra. No '4T', '15B' ni '11/9'."""
    return bool(re.search(r"/\s*[A-Za-z]{1,3}\s*$", (raw or "").strip()))


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


@lru_cache(maxsize=1)
def _caba_locality_cues() -> frozenset[str]:
    return frozenset({"caba", "capital federal", "ciudad autonoma de buenos aires"})


@lru_cache(maxsize=1)
def _province_ba_locality_cues() -> frozenset[str]:
    return frozenset({"buenos aires", "bs as", "bsas", "provincia de buenos aires"})


def _query_wants_caba(text: str) -> bool:
    folded = fold(normalize_text(text))
    return any(cue in folded for cue in _caba_locality_cues())


def _candidate_locality_labels(cand: Candidate) -> set[str]:
    return {
        fold(normalize_text(p))
        for p in (cand.city, cand.district, cand.state) if p
    }


def _candidate_in_caba(cand: Candidate) -> bool:
    skip = {"de", "la", "el", "del", "los", "las", "the", "and", "city"}
    expanded = _expand_locality(_candidate_locality_labels(cand)) - skip
    caba = _expand_locality(set(_caba_locality_cues())) - skip
    return bool(expanded & caba)


def _candidate_province_only_ba(cand: Candidate) -> bool:
    """Martínez / San Isidro: state=Buenos Aires sin city CABA."""
    skip = {"de", "la", "el", "del", "los", "las", "the", "and", "city"}
    expanded = _expand_locality(_candidate_locality_labels(cand)) - skip
    prov = _expand_locality(set(_province_ba_locality_cues())) - skip
    caba = _expand_locality(set(_caba_locality_cues())) - skip
    return bool(expanded & prov) and not bool(expanded & caba)


@lru_cache(maxsize=1)
def _place_skip() -> frozenset[str]:
    """País, tipo de unidad admin y glue: no discriminan ciudad."""
    from ..resources import (
        admin_unit_words, load_json, locality_alias_groups, name_glue_words,
        phone_region_country_map,
    )

    skip: set[str] = set(name_glue_words()) | {fold(w) for w in admin_unit_words()}
    skip.update({"de", "la", "el", "del", "los", "las", "the", "and", "city", "of"})
    countries: set[str] = set()
    iso = load_json("iso3166_alpha2.json")
    for name in iso.values():
        countries.add(fold(str(name)))
    for name in phone_region_country_map().values():
        countries.add(fold(str(name)))
    skip |= countries
    for group in locality_alias_groups():
        if group & countries:
            skip.update(x for x in group if len(x) >= 3)
    return frozenset(skip)


@lru_cache(maxsize=1)
def _numbered_admin_re() -> re.Pattern[str]:
    from ..resources import admin_unit_words

    words = "|".join(
        re.escape(fold(w)) for w in sorted(admin_unit_words(), key=len, reverse=True)
    )
    return re.compile(
        rf"\b(\d{{1,2}})(?:e|er|eme|st|nd|rd|th)?\s+(?:{words})\b"
        rf"|(?:{words})\s+(?:n(?:o|um)?\.?\s*)?(\d{{1,2}})\b"
    )


def _numbered_admin_id(text: str) -> str | None:
    """'Paris 6e Arrondissement' / 'Comuna 2' / '12th District' → '6' / '2' / '12'."""
    if not text:
        return None
    match = _numbered_admin_re().search(fold(normalize_text(text)))
    if not match:
        return None
    return match.group(1) or match.group(2)


def _query_place_labels(text: str) -> set[str]:
    """Ciudades nombradas: alias_groups + segmentos entre comas (Fredericia, Maipú)."""
    from ..resources import locality_alias_groups

    skip = _place_skip()
    folded = fold(normalize_text(text))
    padded = f" {folded} "
    labels: set[str] = set()
    for group in locality_alias_groups():
        for alias in group:
            if len(alias) >= 3 and alias not in skip and f" {alias} " in padded:
                labels.add(alias)
    labels |= _comma_place_labels(text)
    extra: set[str] = set()
    for label in labels:
        stripped = _strip_place_prefix(label)
        if stripped != label and len(stripped) >= 3:
            extra.add(stripped)
    return labels | extra


_POSTCODE_PART = re.compile(
    r"^(?:[a-z]?\d{4,6}[a-z]{0,3}|\d{3,6}(?:\s+\d{2,4})?)$",
    re.IGNORECASE,
)
_HOUSE_PART = re.compile(
    r"^\d+[a-z]{0,3}(?:-\d+[a-z]{0,3}|/[a-z]{1,3})?$", re.IGNORECASE)
_REGION_LEADERS = frozenset({
    "region", "regions", "provincia", "province", "estado", "state",
    "departamento", "department", "metropolitana", "metropolitan",
    "comunidad", "community", "de", "del",
})


def _ordered_comma_places(text: str) -> list[str]:
    """Ciudades en orden: 'Sadowa, 3, Radom, 26-604, Warszawa' → [radom, warszawa].

    El primer segmento es la calle. El primero de esta lista es el destino.
    """
    from ..resources import unit_hints

    skip = _place_skip()
    unit = {fold(h) for h in unit_hints()} | {"apt", "apto", "apartment"}
    labels: list[str] = []
    seen: set[str] = set()
    parts = [p.strip() for p in re.split(r"[,;]", text or "") if p.strip()]
    for i, part in enumerate(parts):
        folded = fold(normalize_text(part))
        if not folded:
            continue
        compact = folded.replace(" ", "")
        if _POSTCODE_PART.match(compact) or _HOUSE_PART.match(folded):
            continue
        if not any(ch.isalpha() for ch in folded):
            continue  # '11-9' → '11 9': manzana-lote, no ciudad
        words = folded.split()
        if not words:
            continue
        # '15/B' → '15 b': esponente, no ciudad
        if all(w.isdigit() or (len(w) == 1 and w.isalpha()) for w in words):
            continue
        if any(w in unit for w in words):
            continue
        if i == 0:
            continue
        if words[0] in _REGION_LEADERS or words[0] in skip:
            continue
        content = [w for w in words if w not in skip and len(w) >= 2]
        if not content:
            continue
        if folded in skip or len(folded) < 3 or folded in seen:
            continue
        seen.add(folded)
        labels.append(folded)
    return labels


def _comma_place_labels(text: str) -> set[str]:
    """'…, Fredericia, 7000, Copenhagen' → {fredericia} (no CP, no país, no región)."""
    return set(_ordered_comma_places(text))


def _strip_place_prefix(label: str) -> str:
    """'former toronto' → 'toronto'. No inventa ciudades, solo saca el adjetivo."""
    stripped = _PLACE_PREFIX.sub("", label or "").strip()
    return stripped or (label or "")


def _place_lookup_names(label: str) -> list[str]:
    folded = fold(normalize_text(label or ""))
    if not folded:
        return []
    names = [folded]
    stripped = _strip_place_prefix(folded)
    if stripped and stripped != folded:
        names.append(stripped)
    return names


def _named_place_far_from_candidate(parsed: ParsedAddress, cand: Candidate) -> bool:
    """True si la ciudad de entrega (primer segmento) queda lejos del pin.

    GeoNames ausente o pueblo desconocido → False: no se veta Toronto vacío.
    """
    from .city_lookup import lookup_city_centroid

    places = _ordered_comma_places(parsed.original or parsed.normalized)
    if not places:
        return False
    # Un barrio conocido de la ciudad del candidato no es otra ciudad. Sin esto
    # 'Av. Santa Fe 3200, Palermo, Buenos Aires' (export de Mercado Libre) se
    # vetaba contra Palermo de Sicilia, y 'Flores' contra Brasil.
    # (Medido 2026-09-15: relajar a "lejos de TODOS los homonimos" tambien
    # destrababa 'San José' de Costa Rica — vetado hoy contra San Jose de
    # California — pero exponia matches flojos de calles numeradas. Queda
    # propuesto, no aplicado: ver examples/geocode-truth/regression.)
    cand_labels = _candidate_place_labels(cand)
    if cand_labels and _expansion_place_overlap({places[0]}, cand_labels):
        return False
    for name in _place_lookup_names(places[0]):
        centroid = lookup_city_centroid(name)
        if centroid is None:
            continue
        return haversine_km(centroid[0], centroid[1], cand.lat, cand.lon) > PLACE_FAR_KM
    return False


def _haystack_place_labels(text: str) -> set[str]:
    """Ciudades que aparecen en el texto OSM aunque addr:city esté vacío."""
    from ..resources import locality_alias_groups

    folded = fold(normalize_text(text or ""))
    if not folded:
        return set()
    skip = _place_skip()
    padded = f" {folded} "
    labels: set[str] = {folded}
    for group in locality_alias_groups():
        for alias in group:
            if len(alias) >= 3 and alias not in skip and f" {alias} " in padded:
                labels.add(alias)
    return labels


def _candidate_place_labels(cand: Candidate) -> set[str]:
    """city/district solamente: el state (NY, Île-de-France, PBA) es demasiado ancho."""
    skip = _place_skip()
    labels: set[str] = set()
    for raw in (cand.city, cand.district):
        if not raw:
            continue
        folded = fold(normalize_text(raw))
        if folded not in skip and len(folded) >= 3:
            labels.add(folded)
        for tok in folded.split():
            if tok not in skip and len(tok) >= 3:
                labels.add(tok)
    return labels


def _place_labels_overlap(query: set[str], cand: set[str]) -> bool:
    if _expand_locality(query) & _expand_locality(cand):
        return True
    # 'buenos aires' vive dentro de 'ciudad autonoma de buenos aires'
    for q in query:
        if len(q) < 5:
            continue
        needle = f" {q} "
        for c in cand:
            hay = f" {c} "
            if needle in hay or hay in needle:
                return True
    return _expansion_place_overlap(query, cand)


def _expansion_place_overlap(query: set[str], cand: set[str]) -> bool:
    """Belgrano (barrio) cuenta como CABA. No usa GeoNames (demasiado ancho)."""
    from ..resources import load_json

    rows = load_json("locality_expand.json", "SMART_IMPORT_LOCALITY_EXPAND_PATH").get(
        "expansions") or ()
    q = query | _expand_locality(query)
    c = cand | _expand_locality(cand)
    for row in rows:
        if not isinstance(row, dict):
            continue
        cues = {fold(str(x)) for x in (row.get("cues") or ()) if str(x).strip()}
        tokens = {fold(str(x)) for x in (row.get("tokens") or ()) if str(x).strip()}
        if not cues:
            continue
        if query & cues and c & (tokens | cues):
            return True
        if cand & cues and q & (tokens | cues):
            return True
    return False


def place_conflict(parsed: ParsedAddress, cand: Candidate) -> bool:
    """True si la query nombra un lugar y el candidato es otro (homónimo).

    No es un if por país: CP distinto, arrondissement/comuna distinto, o
    ciudad disjunta. Calle+altura en Versailles no es la de París 6e.

    El nombre de la calle no cuenta como ciudad: 'Calea Bucuresti, Sinaia' no
    es Bucarest. OSM sin addr:city no se veta por vacío ( tumba Toronto ); se
    veta si GeoNames dice que el destino está lejos del pin (Holstebro,
    Punta Arenas). Varsovia inyectada como región no salva un pin en Radom.
    """
    if parsed.postcode and cand.postcode:
        return not _postcodes_compatible(parsed.postcode, cand.postcode)

    q_admin = _numbered_admin_id(parsed.original or parsed.normalized)
    c_admin = _numbered_admin_id(" ".join(
        p for p in (cand.city, cand.district) if p
    ))
    if q_admin and c_admin and q_admin != c_admin:
        return True

    query = _query_place_labels(parsed.original or parsed.normalized)
    city = _candidate_place_labels(cand)
    far = _named_place_far_from_candidate(parsed, cand)

    if city:
        if query and not _place_labels_overlap(query, city):
            return True
        return far

    if not query:
        return False
    hay = " ".join(p for p in (cand.normalized_text, cand.name) if p)
    if _place_labels_overlap(query, _haystack_place_labels(hay)):
        return False
    return far


def _locality_score(text: str, cand: Candidate) -> float:
    """CABA en la query tiene que matchear 'Ciudad Autónoma de Buenos Aires'.

    No se iguala CABA con la provincia: son grupos distintos en locality_expand.
    Si la query pide CABA explícito, un candidato solo provincial (Martínez) no
    puede sumar locality=1.0 aunque 'Buenos Aires' aparezca suelto en el texto.
    """
    cand_labels = _candidate_locality_labels(cand)
    if not cand_labels:
        return 0.0
    skip = {"de", "la", "el", "del", "los", "las", "the", "and", "city"} | set(_place_skip())
    query = _expand_locality(_locality_labels(text)) - skip
    city = _expand_locality(cand_labels) - skip

    if _query_wants_caba(text):
        if _candidate_in_caba(cand):
            return 1.0
        if _candidate_province_only_ba(cand):
            return 0.0

    return 1.0 if query & city else 0.0


def _house_number_match(parsed: ParsedAddress, cand: Candidate) -> float:
    if not (parsed.house_number and cand.house_number):
        return 0.0
    want = parsed.house_number.strip().lower()
    got = cand.house_number.strip().lower()
    if want == got:
        return 1.0
    if _house_key(want) == _house_key(got):
        # 4T / 4bis / 3845A no son el 4 / 3845 de otra parcela.
        # '15/B' vs '15': el interno slash+letra es la misma puerta.
        # '15/A' vs '15/B' queda parcial.
        if _house_suffix(want) == _house_suffix(got):
            return 1.0
        if _has_letter_intern(want) and not _house_suffix(got):
            return 1.0
        if _has_letter_intern(got) and not _house_suffix(want):
            return 1.0
        return 0.5
    if want.rstrip(_HOUSE_LETTERS) == got.rstrip(_HOUSE_LETTERS):
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
