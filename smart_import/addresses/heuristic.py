"""Parser de direcciones por reglas. Sin datos externos, sin pais fijo.

Cubre las dos formas dominantes sin privilegiar ninguna:

    LatAm / ES   `Av. Corrientes 100`      road + house_number
    EN / IN      `23 MG Road`              house_number + road
    EN ordinal   `1171 1st Ave`            house_number + ordinal road
    BR           `Av. Paulista, 1578`      road + coma + altura
    LatAm num.   `Calle 50 nro 1234`       via numerada + altura

Lo que no entiende lo deja en `landmark`/`neighbourhood` en vez de descartarlo:
perder "Near Hanuman Temple" empeora el geocoding mas que conservarlo.

Prefijos / glue: `resources/geo_keywords.json` (no hardcodear listas aca).
"""
from __future__ import annotations

import re
from functools import lru_cache

from ..resources import (
    building_tokens,
    fold,
    label_set,
    landmark_prefixes,
    load_json,
    locality_alias_groups,
    name_glue_words,
    neighbourhood_prefixes,
    road_narration_words,
    suspicious_road_words,
    unit_prefixes,
)
from .base import AddressParser, ParsedAddress
from .gate import road_is_suspicious

_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)

# Un numero de 4 digitos en el medio de una direccion es la altura, no el CP. Solo
# se acepta como codigo postal el CPA argentino, un pincode/ZIP de 5-6, o 4 digitos
# al final del ultimo segmento.
_POSTCODE = re.compile(r"(?<![\w])(?P<pc>[A-Z]\d{4}[A-Z]{3}|\d{5,6})(?![\w])")
_POSTCODE_TRAILING = re.compile(r"(?<![\w])(?P<pc>\d{4})\s*$")
_LEVEL = re.compile(r"(?<![\w])(?P<v>\d{1,2}(?:st|nd|rd|th|do|ro|to|er|°|º)?)\s+"
                    r"(?:floor|andar|piso)\b", re.IGNORECASE)

# 'Calle 50 nro 1234' / 'Carrera 7 # 45-10' — la via LLEVA un numero en el nombre.
_NUMBERED_STREET = re.compile(
    r"(?P<road>(?:calle|carrera|cl\.?|cra\.?|diagonal|diag\.?|"
    r"transversal|tv\.?|avenida|av\.?|rua|r\.?)\s+\d{1,4})"
    r"[\s,]+(?:nro\.?|n[°º]?|num\.?|núm\.?|#)?\s*(?P<num>\d{1,5}[A-Za-z]?)"
    r"(?:-\d{1,4})?(?![\d])",
    re.IGNORECASE | re.UNICODE)

# 'Corrientes 100' / 'Santa Fe al 137' / 'Av. Paulista, 1578'
# El prefijo numerico opcional deja pasar calles que se llaman con un numero:
# '11 de Septiembre 1913', '9 de Julio 250', '25 de Mayo 100'.
# `[\s,]+` acepta la coma brasilena entre calle y altura.
_ROAD_THEN_NUMBER = re.compile(
    r"(?P<road>(?:\d{1,3}\s+(?:de|of)\s+)?"
    r"(?:[^\W\d_][\w'’.\-]*\s+){0,4}[^\W\d_][\w'’.\-]*)"
    r"[\s,]+(?:al\s+|n[°º]?\s*|nro\.?\s*|num\.?\s*|#\s*)?(?P<num>\d{1,5}[A-Za-z]?)"
    r"(?![\d/])",
    re.IGNORECASE | re.UNICODE)
#: conectores que quedan pegados al final de la calle y no son parte del nombre
_ROAD_TAIL = re.compile(r"[\s,]+(?:al|nro|nro\.|n[°º]|num|num\.|numero|número|#)$",
                        re.IGNORECASE)

# '1171 1st Ave' / '350 5th Avenue' — ordinales EN que empiezan con digito.
_NUMBER_THEN_ORDINAL_ROAD = re.compile(
    r"(?<!\d)(?P<num>\d{1,5}[A-Za-z]?)\s+"
    r"(?P<road>\d{1,3}(?:st|nd|rd|th)\.?\s+"
    r"[^\W\d_][\w'’.\-]*(?:\s+[^\W\d_][\w'’.\-]*){0,2})",
    re.IGNORECASE | re.UNICODE)

# '350 NE 1st Ave' / '1200 NW 7th Ave' — cardinal US + ordinal (NE/NW no es la calle).
_CARDINAL = r"(?:N|S|E|W|NE|NW|SE|SW|N\.|S\.|E\.|W\.)"
_NUMBER_THEN_CARDINAL_ORDINAL = re.compile(
    r"(?<!\d)(?P<num>\d{1,5}[A-Za-z]?)\s+"
    r"(?P<road>" + _CARDINAL + r"\s+"
    r"\d{1,3}(?:st|nd|rd|th)\.?\s+"
    r"[^\W\d_][\w'’.\-]*)",
    re.IGNORECASE | re.UNICODE)

# '23 MG Road' / '507 Broadway' — solo whitespace: la coma separa segmentos
# ('3, Ciudad de la Costa' NO es number+road).
_NUMBER_THEN_ROAD = re.compile(
    r"(?<!\d)(?P<num>\d{1,5}[A-Za-z]?)\s+(?P<road>[^\W\d_][\w'’.\-]*"
    r"(?:\s+[^\W\d_][\w'’.\-]*){0,3})", re.IGNORECASE | re.UNICODE)

_SPLIT = re.compile(r"\s*[,;|]\s*")

# Orden: patrones especificos primero (numbered, ordinal), luego los generales.
_STREET_PATTERNS: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (_NUMBERED_STREET, "numbered-street", True),       # trim road
    (_NUMBER_THEN_CARDINAL_ORDINAL, "number+cardinal-ordinal", False),
    (_NUMBER_THEN_ORDINAL_ROAD, "number+ordinal", False),
    (_ROAD_THEN_NUMBER, "road+number", True),
    (_NUMBER_THEN_ROAD, "number+road", False),
)


def _alt(words) -> str:
    parts = sorted({str(w) for w in words if w}, key=len, reverse=True)
    if not parts:
        return "(?!)"
    return "(?:%s)" % "|".join(re.escape(w) for w in parts)


@lru_cache(maxsize=1)
def _unit_re() -> re.Pattern[str]:
    return re.compile(
        rf"(?<![^\W\d_]){_alt(unit_prefixes())}\b\s*"
        r"(?P<v>[\w][\w\-/]{0,7}(?:\s+[A-Za-z](?![\w]))?)",
        re.IGNORECASE,
    )


@lru_cache(maxsize=1)
def _landmark_re() -> re.Pattern[str]:
    return re.compile(
        rf"(?<![^\W\d_]){_alt(landmark_prefixes())}\s+(?P<v>[^,;.|]{{2,60}})",
        re.IGNORECASE,
    )


@lru_cache(maxsize=1)
def _neighbourhood_re() -> re.Pattern[str]:
    return re.compile(
        rf"(?<![^\W\d_]){_alt(neighbourhood_prefixes())}\s+(?P<v>[^,;.|]{{2,40}})",
        re.IGNORECASE,
    )


def _trim_road(road: str, tokens: frozenset[str]) -> str:
    """Recorta el relleno de la izquierda: 'que vive en Av. Corrientes' -> 'Av. Corrientes'.

    Antes se exigia que la palabra fuera capitalizada o token de via. Los exports
    reales no cumplen eso —'Av cabildo 834', 'Av. Ramos mejia 1358', 'Av  diaz
    velez 5508'— y una sola minuscula tiraba la direccion entera: 19% de las que
    SI tenian altura quedaban sin `road`.

    La regla es la inversa y funciona con datos sucios: se acepta toda palabra
    alfabetica salvo las de narracion, que son un conjunto chico y cerrado.
    """
    glue = name_glue_words()
    narracion = road_narration_words()
    words = road.split()
    keep = len(words)
    ultimo = len(words) - 1
    for i in range(ultimo, -1, -1):
        word = words[i].strip(".,;:")
        folded = fold(word)
        if not word:
            break
        if folded in narracion:
            break                                      # empezo la narracion
        if folded in tokens:
            keep = i
            # En ES/PT el tipo de via ENCABEZA el nombre ('Av. Cabildo'), asi que
            # a su izquierda ya no hay calle: corta el nombre del cliente pegado
            # adelante ('Mariana Gonzalez av cabildo'). En EN/IN el tipo va al
            # final ('MG Road'): ahi hay que seguir hacia la izquierda.
            if i != ultimo:
                break
            continue
        if word.isdigit() or _LETTER.search(word):
            keep = i
            continue
        if folded in glue and keep <= i + 1 and i > 0:
            continue                                   # 'de' entre dos partes del nombre
        break
    return " ".join(words[keep:]).strip(" .,;:-")


def _street_candidates(haystack: str, tokens: frozenset[str]
                       ) -> list[tuple[int, int, int, str, str, str]]:
    """Candidatos (prioridad, begin, end, road, num, order) sobre TODO el texto."""
    out: list[tuple[int, int, int, str, str, str]] = []
    for pattern, order_name, do_trim in _STREET_PATTERNS:
        position = 0
        while (match := pattern.search(haystack, position)) is not None:
            position = max(match.start("num"), position + 1)
            whole = _ROAD_TAIL.sub("", match.group("road").strip(" .,;:-"))
            road = _trim_road(whole, tokens) if do_trim else whole
            number = match.group("num").strip()
            if not road or (fold(road) in tokens and len(road.split()) == 1):
                continue
            # No anclar en 'Casa 5' / 'Plot No 42' — el gate pedira enhancer.
            if road_is_suspicious(road):
                continue
            has_token = any(fold(w.strip(".,;:")) in tokens for w in road.split())
            # Ordinales y vias numeradas siempre ganan sobre el generico.
            boost = -4 if pattern is _NUMBERED_STREET else (
                -4 if pattern is _NUMBER_THEN_CARDINAL_ORDINAL else (
                -3 if pattern is _NUMBER_THEN_ORDINAL_ROAD else (-2 if has_token else 0)))
            road_start = match.start("road") + max(match.group("road").find(road), 0)
            if pattern in (_NUMBER_THEN_ROAD, _NUMBER_THEN_ORDINAL_ROAD,
                           _NUMBER_THEN_CARDINAL_ORDINAL):
                begin, end = match.start("num"), match.end("road")
            else:
                begin, end = road_start, match.end("num")
            out.append((boost, begin, max(end, begin), road, number, order_name))
    return out


def find_street_span(text: str, locales: tuple[str, ...] | None = None
                     ) -> tuple[int, int] | None:
    """Donde esta el 'calle + altura' dentro de un texto mas largo.

    Es el ancla del extractor de direcciones: alrededor de esto se expande hasta
    el borde de la clausula, en vez de quedarse con la frase entera.
    """
    haystack = text or ""
    tokens = label_set("street_tokens", locales)
    candidates = _street_candidates(haystack, tokens)
    if not candidates:
        return None
    best = min(candidates, key=lambda c: (c[0], c[1]))
    return (best[1], best[2])


class HeuristicAddressParser(AddressParser):
    """Implementacion por defecto: rapida, sin dependencias, sin datos."""

    name = "heuristic"

    def __init__(self, locales: tuple[str, ...] | None = None):
        self.locales = locales

    def parse(self, text: str, context=None) -> ParsedAddress:
        raw = (text or "").strip()
        if not raw:
            return ParsedAddress(text=raw, parser=self.name)

        components: dict[str, str] = {}
        evidence: list[str] = []
        consumed: list[tuple[int, int]] = []

        def take(match: re.Match | None, name: str, group: str = "v", why: str = "") -> None:
            if not match or components.get(name):
                return
            value = match.group(group).strip(" .,;:-")
            if not value:
                return
            if any(_overlaps(match.span(), span) for span in consumed):
                return                       # ya lo tomo otro campo (p.ej. la altura)
            components[name] = value
            consumed.append(match.span())
            evidence.append(why or f"{name} '{value}'")

        # La CALLE primero: un '03845' pegado al nombre de calle es la altura,
        # no un ZIP. Si el CP se lleva el numero antes, la direccion queda sin
        # calle Y sin altura ('AV JUAN DE GARAY 03845' -> postcode + nada mas).
        self._take_street(raw, consumed, components, evidence)
        self._take_bare_road(raw, consumed, components, evidence)
        take(_POSTCODE.search(raw), "postcode", "pc")
        take(_LEVEL.search(raw), "level")
        take(_unit_re().search(raw), "unit")
        take(_landmark_re().search(raw), "landmark")
        take(_neighbourhood_re().search(raw), "neighbourhood")

        self._take_trailing_postcode(raw, consumed, components, evidence)
        self._take_localities(raw, consumed, components, evidence)

        return ParsedAddress(text=raw, components=components, parser=self.name,
                             evidence=tuple(evidence))

    # ---------- calle ----------

    def _take_street(self, raw: str, consumed, components, evidence) -> None:
        """Busca calle+altura en TODOS los segmentos, no solo el primero.

        Asi 'Apt 4B, 350 5th Ave' y 'Near X, Av. Paulista, 1578' resuelven bien.
        """
        tokens = label_set("street_tokens", self.locales)
        candidates = [
            c for c in _street_candidates(raw, tokens)
            if not any(_overlaps((c[1], c[2]), span) for span in consumed)
        ]
        if not candidates:
            return
        _boost, begin, end, road, number, order = min(
            candidates, key=lambda c: (c[0], c[1]))
        components["road"] = road
        components["house_number"] = number
        consumed.append((begin, end))
        evidence.append(f"'{road}' + numero '{number}' ({order})")

    def _take_bare_road(self, raw: str, consumed, components, evidence) -> None:
        """Calle sin altura: 'Yerbal, CABA' / 'Av. Cabildo, Argentina'.

        El patron calle+numero no aplica. Sin esto el primer segmento cae en
        suburb y el gate pide libpostal solo para copiar la misma palabra a
        `road`. Barrios y paises conocidos no se roban como calle.
        """
        if components.get("road"):
            return
        parts = [p.strip() for p in _SPLIT.split(raw) if p.strip()]
        if len(parts) < 2:
            return
        first = parts[0]
        if not _looks_like_bare_road(first, self.locales):
            return
        start = raw.find(first)
        if start < 0:
            return
        span = (start, start + len(first))
        if any(_overlaps(span, other) for other in consumed):
            return
        road = first.strip(" .,;:-")
        components["road"] = road
        consumed.append(span)
        evidence.append(f"'{road}' (calle sin altura)")

    def _take_trailing_postcode(self, raw, consumed, components, evidence) -> None:
        """CP de 4 digitos al final. No reutiliza la altura de la calle.

        'Av. Paulista, 1578' → 1578 es house_number, no postcode.
        'Av. Pueyrredon 359, Recoleta, 1425' → 1425 si es CP.
        """
        if components.get("postcode"):
            return
        match = _trailing_postcode(raw)
        if not match:
            return
        value = match.group("pc")
        if value == components.get("house_number"):
            return
        if any(_overlaps(match.span(), span) for span in consumed):
            return
        components["postcode"] = value
        consumed.append(match.span())
        evidence.append(f"postcode '{value}'")

    # ---------- barrio / ciudad ----------

    def _take_localities(self, raw: str, consumed, components, evidence) -> None:
        """Los segmentos que sobran, en orden: barrio -> ciudad."""
        rest: list[str] = []
        offset = 0
        for part in _SPLIT.split(raw):
            start = raw.index(part, offset) if part else offset
            offset = start + len(part)
            cleaned = _POSTCODE.sub("", part).strip(" .,;:-()")
            if not cleaned or any(_overlaps((start, offset), span) for span in consumed):
                continue
            if len(cleaned.split()) > 5:
                continue
            rest.append(cleaned)

        slots = [name for name in ("suburb", "city") if not components.get(name)]
        for name, value in zip(slots, rest):
            components[name] = value
            evidence.append(f"{name} '{value}' (segmento posicional)")


def _trailing_postcode(raw: str) -> re.Match | None:
    """4 digitos como ultimo segmento: 'Av. Pueyrredon 359, Recoleta, 1425'."""
    if "," not in raw:
        return None
    tail_start = raw.rindex(",") + 1
    match = _POSTCODE_TRAILING.search(raw, tail_start)
    return match


def _looks_like_bare_road(segment: str, locales: tuple[str, ...] | None) -> bool:
    """True para 'Yerbal' o 'Av. Cabildo'; False para 'Palermo' o 'Flat 14B'."""
    cleaned = (segment or "").strip(" .,;:-")
    if not cleaned or road_is_suspicious(cleaned, locales):
        return False
    words = [w.strip(".,;:") for w in cleaned.split() if w.strip(".,;:")]
    if not words or len(words) > 3:
        return False
    folded_words = [fold(w) for w in words]
    blocked = suspicious_road_words() | building_tokens() | {
        fold(p) for p in (*unit_prefixes(), *neighbourhood_prefixes(),
                          *landmark_prefixes())
    }
    if any(w in blocked for w in folded_words):
        return False
    tokens = label_set("street_tokens", locales)
    # 'Av. Cabildo' / 'Cabildo Ave': el tipo de via encabeza o cierra, no va
    # en el medio de 'Near Hanuman Temple'.
    if folded_words[0] in tokens or folded_words[-1] in tokens:
        return True
    if len(words) != 1:
        return False
    word = words[0]
    if any(ch.isdigit() for ch in word) or not _LETTER.search(word):
        return False
    if len(word) < 4:
        return False
    return fold(word) not in _locality_blocklist()


@lru_cache(maxsize=1)
def _locality_blocklist() -> frozenset[str]:
    """Barrios/ciudades/paises curados: no son nombre de calle suelto."""
    labels: set[str] = set()
    for group in locality_alias_groups():
        labels.update(item for item in group if item and " " not in item)
    data = load_json("locality_expand.json", "SMART_IMPORT_LOCALITY_EXPAND_PATH")
    for row in data.get("expansions") or ():
        if not isinstance(row, dict):
            continue
        for cue in row.get("cues") or ():
            folded = fold(str(cue))
            if folded and " " not in folded:
                labels.add(folded)
    return frozenset(labels)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]
