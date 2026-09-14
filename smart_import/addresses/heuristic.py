"""Parser de direcciones por reglas. Sin datos externos, sin pais fijo.

Cubre las dos formas dominantes sin privilegiar ninguna:

    LatAm / ES   `Av. Corrientes 100`      road + house_number
    EN / IN      `23 MG Road`              house_number + road
    EN ordinal   `1171 1st Ave`            house_number + ordinal road
    BR           `Av. Paulista, 1578`      road + coma + altura
    LatAm num.   `Calle 50 nro 1234`       via numerada + altura
    LatAm abbr.  `CR 55, 93F-07` / `CL 3 SUR`
    EN grid     `68 ST` / `E 2 ST` / `BCH 26 ST`  via numerada EN + altura
    EN hyphen   `108-15 Jamaica Ave`              altura Queens / Bronx
    Esponente    `Viale X, 15/B`                  altura + / + letra (no es Italia-only)
    JP OA        `京浜島二丁目, 11-9, 大田区`       chome + manzana-lote + ward

Lo que no entiende lo deja en `landmark`/`neighbourhood` en vez de descartarlo:
perder "Near Hanuman Temple" empeora el geocoding mas que conservarlo.

Prefijos / glue: `resources/geo_keywords.json` (no hardcodear listas aca).
"""
from __future__ import annotations

import re
import unicodedata
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
from ..text_script import has_cjk
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

# Altura: 100 / 100A / 3922BIS / Queens 108-15 / Bronx 4601-B21 / placa 93F-07
# / esponente 15/B (slash + letra; no 15/2 ni 11-9).
# No comerse '7th'/'1st' (ordinales EN) como si fueran sufijo de puerta.
_HOUSE_NUM_CORE = (
    r"\d{1,5}"
    r"(?:(?!(?:st|nd|rd|th)\b)[A-Za-z]{1,3})?"
    r"(?:-\d{1,4}|-[A-Za-z]\d{1,3}|/[A-Za-z]{1,3})?"
    r"[A-Za-z]?"
)
_HOUSE_NUM = rf"(?P<num>{_HOUSE_NUM_CORE})"

# 'Calle 50 nro 1234' / 'CR 55, 93F-07' / 'CL 3 SUR' — la via LLEVA un numero.
# Alternation izquierda-primero: cra antes de cr, calle antes de cl.
# `(?<!\w)` evita que `r` (rua) se coma la R de `CR 55` → road='R 55'.
_VIA_NUMBERED = (
    r"calle|carrera|diagonal|transversal|avenida|"
    r"cra|cr|cl|dg|diag|tv|av|rua|r"
)
_NUMBERED_STREET = re.compile(
    r"(?P<road>(?<!\w)(?:" + _VIA_NUMBERED + r")\.?\s+"
    r"\d{1,4}[A-Za-z]{0,3}"
    r"(?:\s+(?:sur|norte|este|oeste)\b)?)"
    r"[\s,]+(?:nro\.?|n[°º]?|num\.?|núm\.?|#)?\s*" + _HOUSE_NUM + r"(?![\d])",
    re.IGNORECASE | re.UNICODE)

# 'Corrientes 100' / 'Santa Fe al 137' / 'Av. Paulista, 1578'
# El prefijo numerico opcional deja pasar calles que se llaman con un numero:
# '11 de Septiembre 1913', '9 de Julio 250', '25 de Mayo 100'.
# `[\s,]+` acepta la coma brasilena entre calle y altura.
_ROAD_THEN_NUMBER = re.compile(
    r"(?P<road>(?:\d{1,3}\s+(?:de|of)\s+)?"
    r"(?:[^\W\d_][\w'’.\-]*\s+){0,4}[^\W\d_][\w'’.\-]*)"
    r"[\s,]+(?:al\s+|n[°º]?\s*|nro\.?\s*|num\.?\s*|#\s*)?" + _HOUSE_NUM + r"(?![\d])",
    re.IGNORECASE | re.UNICODE)
#: conectores que quedan pegados al final de la calle y no son parte del nombre
_ROAD_TAIL = re.compile(r"[\s,]+(?:al|nro|nro\.|n[°º]|num|num\.|numero|número|#)$",
                        re.IGNORECASE)

# Los tres patrones `number+road` de abajo arrancan con `_NUM_START`, que exige
# que la altura NO venga pegada a otra palabra.
#
# Con `(?<!\d)` la altura solo tenia prohibido venir pegada a otro DIGITO, no a
# una letra. En un paste de despacho eso alcanza para arruinar la direccion:
#
#   'mensajero1 Av Corrientes 1800 2B'  ->  road='Av Corrientes'  altura='1'
#
# El '1' de 'mensajero1' pasaba el lookbehind (antes hay una 'o'), armaba el
# candidato number+road 'Av Corrientes' + '1', empataba en boost con el
# candidato correcto (los dos tienen el token de via 'av') y el desempate es
# por posicion: gana el que empieza mas a la izquierda, o sea el falso.
#
# Es el peor error del extractor porque no pierde un dato: pone un pin CONFIADO
# treinta cuadras mas aca. Y dispara con todo lo que abunda en un despacho real:
# ids de mensajero, moviles, rutas, zonas, numeros de pedido ('movil3', 'zona2',
# 'Pedido123'). `(?<!\w)` es un superconjunto estricto de `(?<!\d)`: no habilita
# ningun match nuevo, solo saca estos falsos positivos.
#
# La excepcion es el marcador de numero: en 'Nº1234 Calle Falsa' la altura SI
# viene pegada a una palabra, y es la altura. Python cuenta 'º' y 'ª' como \w
# (el '°' de grados no), asi que sin esta alternativa `(?<!\w)` se las comia.
_NUM_START = r"(?:(?<!\w)|(?<=[nN][ºª°]))"

# '1171 1st Ave' / '350 5th Avenue' — ordinales EN que empiezan con digito.
_NUMBER_THEN_ORDINAL_ROAD = re.compile(
    _NUM_START + _HOUSE_NUM + r"\s+"
    r"(?P<road>\d{1,3}(?:st|nd|rd|th)\.?\s+"
    r"[^\W\d_][\w'’.\-]*(?:\s+[^\W\d_][\w'’.\-]*){0,2})",
    re.IGNORECASE | re.UNICODE)

# '350 NE 1st Ave' / '1200 NW 7th Ave' — cardinal US + ordinal (NE/NW no es la calle).
# Palabra entera: sin \b, la 's' de 'united states 219 ST' y la 'N' de
# 'BRIGHTON 7 ST' se comen como S/N y el geocoder busca South 219th / North 7th.
_CARDINAL = r"(?:\b(?:NE|NW|SE|SW|N|S|E|W)\b|\b(?:N|S|E|W)\.)"
_NUMBER_THEN_CARDINAL_ORDINAL = re.compile(
    _NUM_START + _HOUSE_NUM + r"\s+"
    r"(?P<road>" + _CARDINAL + r"\s+"
    r"\d{1,3}(?:st|nd|rd|th)\.?\s+"
    r"[^\W\d_][\w'’.\-]*)",
    re.IGNORECASE | re.UNICODE)

# '23 MG Road' / '507 Broadway' / '108-15 Jamaica Ave'
# Solo whitespace: la coma separa segmentos ('3, Ciudad de la Costa' NO es number+road).
_NUMBER_THEN_ROAD = re.compile(
    _NUM_START + _HOUSE_NUM + r"\s+(?P<road>[^\W\d_][\w'’.\-]*"
    r"(?:\s+[^\W\d_][\w'’.\-]*){0,3})", re.IGNORECASE | re.UNICODE)

# '68 ST' / '5 AVE' / 'E 2 ST' / 'BCH 26 ST' / '150 PL' — la via LLEVA el numero.
# OA NYC escribe 'E 65 ST', no 'E 65th St'; sin esto road+number come el 65.
_EN_WAY = (
    r"(?:st|street|ave|avenue|rd|road|pl|place|ct|court|blvd|boulevard|"
    r"dr|drive|ln|lane|pkwy|parkway|expy|expressway|hwy|highway|"
    r"ter|terrace|cir|circle|pk)"
)
_EN_NUMBERED_ROAD = (
    r"(?:(?:bch|beach|plumb)\s+)?"
    r"(?:" + _CARDINAL + r"\s+)?"
    r"(?:[^\W\d_][\w'’.\-]{2,}\s+)?"
    r"\d{1,3}[A-Za-z]?\s+" + _EN_WAY + r"\.?"
)
#: 'united states 219 ST' (noise 3, país pegado sin coma).
_LEADING_COUNTRY = re.compile(
    r"^(?:(?:united(?:\s+states)?|states|usa|u\.s\.a?\.?|"
    r"estados(?:\s+unidos)?|argentina|españa|espana|france|"
    r"méxico|mexico)\s+)+",
    re.IGNORECASE,
)
_EN_NUMBERED_THEN_HOUSE = re.compile(
    r"(?P<road>" + _EN_NUMBERED_ROAD + r")"
    r"[\s,]+(?:nro\.?|n[°º]?|num\.?|#)?\s*" + _HOUSE_NUM + r"(?![\d])",
    re.IGNORECASE | re.UNICODE)
_HOUSE_THEN_EN_NUMBERED = re.compile(
    _NUM_START + _HOUSE_NUM + r"\s+(?P<road>" + _EN_NUMBERED_ROAD + r")",
    re.IGNORECASE | re.UNICODE)

_SPLIT = re.compile(r"\s*[,;|]\s*")

#: OpenAddresses CABA: 'APELLIDO, NOMBRE, 3645' / 'FLORES, VENANCIO, Gral., 185'.
#: La coma separa partes del nombre de via, no barrio/ciudad.
_HOUSE_ONLY_SEGMENT = re.compile(rf"^{_HOUSE_NUM_CORE}$")
_OA_LEADING_NUMBER_COMMA = re.compile(
    rf"^\s*{_HOUSE_NUM}\s+(?P<tail>.+)$",
    re.UNICODE)
_LOCALITY_SEGMENT = re.compile(
    r"\b(?:ciudad autonoma|capital federal|caba|buenos aires|argentina|"
    r"autonomous city|republica argentina)\b",
    re.IGNORECASE)
#: Titulos / tipos de via en nombres OA CABA ('PENA, DAVID, DR., 4256').
#: Sin esto 'DR.'/'AV.'/'Pr' caen en la regla de codigo pais de 2 letras.
_OA_STREET_SUFFIX = re.compile(
    r"^(?:dr|gral|general|coronel|ing|arq|prof|av|pte|pres|pr|cap|"
    r"virrey|cmdte|ten|sgto|sarg|brig|alm|min)\.?$",
    re.IGNORECASE)

# Orden: patrones especificos primero (numbered, ordinal), luego los generales.
_STREET_PATTERNS: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (_NUMBERED_STREET, "numbered-street", True),       # trim road
    (_EN_NUMBERED_THEN_HOUSE, "en-numbered-street", True),
    (_HOUSE_THEN_EN_NUMBERED, "number+en-numbered", False),
    (_NUMBER_THEN_CARDINAL_ORDINAL, "number+cardinal-ordinal", False),
    (_NUMBER_THEN_ORDINAL_ROAD, "number+ordinal", False),
    (_ROAD_THEN_NUMBER, "road+number", True),
    (_NUMBER_THEN_ROAD, "number+road", False),
)
_NUMBER_FIRST_PATTERNS = (
    _NUMBER_THEN_ROAD, _NUMBER_THEN_ORDINAL_ROAD, _NUMBER_THEN_CARDINAL_ORDINAL,
    _HOUSE_THEN_EN_NUMBERED,
)
_NUMBERED_ROAD_PATTERNS = (
    _NUMBERED_STREET, _EN_NUMBERED_THEN_HOUSE, _HOUSE_THEN_EN_NUMBERED,
    _NUMBER_THEN_CARDINAL_ORDINAL,
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


#: Unidad sin etiqueta INMEDIATAMENTE despues de la altura: 'Av Cabildo 900 1A'.
#: Anclado al final del match de calle+altura (ver `_take_bare_unit`), y con la
#: letra pegada al numero: '2 u' / '3 kg' de paqueteria no entran.
_BARE_UNIT_AFTER_NUMBER = re.compile(
    r"\s+(?P<v>\d{1,3}[A-Za-z]|PB)(?![\w])", re.IGNORECASE)


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
    unit_spans = [m.span() for pattern in (_unit_re(), _LEVEL)
                  for m in pattern.finditer(haystack)]
    for pattern, order_name, do_trim in _STREET_PATTERNS:
        position = 0
        while (match := pattern.search(haystack, position)) is not None:
            position = max(match.start("num"), position + 1)
            if any(_overlaps(match.span(), span) for span in unit_spans):
                continue
            whole = _ROAD_TAIL.sub("", match.group("road").strip(" .,;:-"))
            road = _trim_road(whole, tokens) if do_trim else whole
            road = _LEADING_COUNTRY.sub("", road).strip()
            number = match.group("num").strip()
            if not road or (fold(road) in tokens and len(road.split()) == 1):
                continue
            # No anclar en 'Casa 5' / 'Plot No 42' — el gate pedira enhancer.
            if road_is_suspicious(road):
                continue
            has_token = any(fold(w.strip(".,;:")) in tokens for w in road.split())
            # Ordinales y vias numeradas siempre ganan sobre el generico.
            boost = -4 if pattern in _NUMBERED_ROAD_PATTERNS else (
                -3 if pattern is _NUMBER_THEN_ORDINAL_ROAD else (
                    -2 if has_token else 0))
            road_start = match.start("road") + max(match.group("road").find(road), 0)
            if pattern in _NUMBER_FIRST_PATTERNS:
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
        raw = unicodedata.normalize("NFC", text or "").strip()
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
        self._take_bare_unit(raw, consumed, components, evidence)
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
        oa = _try_oa_compound_street(raw, self.locales)
        if oa is not None:
            begin, end, road, number, order = oa
            if not any(_overlaps((begin, end), span) for span in consumed):
                components["road"] = road
                components["house_number"] = number
                consumed.append((begin, end))
                evidence.append(f"'{road}' + numero '{number}' ({order})")
                return

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

    def _take_bare_unit(self, raw, consumed, components, evidence) -> None:
        """Unidad SIN etiqueta pegada a la altura: 'Av Cabildo 900 1A'.

        `_unit_re` exige prefijo ('depto 4B') a proposito: un '4B' suelto en
        cualquier parte de la frase es demasiado ambiguo. Pero inmediatamente
        despues de la altura no lo es — ahi no hay otra cosa que pueda ser.

        Se ancla en el final del match de calle+altura, y solo acepta la forma
        digito(s)+letra pegadas. Sin este anclaje 'Ruta 2 km 5' o un '2 u' de
        paqueteria entrarian como unidad.
        """
        if components.get("unit") or not components.get("house_number"):
            return
        if not consumed:
            return
        _, fin_calle = consumed[0]
        match = _BARE_UNIT_AFTER_NUMBER.match(raw, fin_calle)
        if not match:
            return
        span = match.span("v")
        if any(_overlaps(span, other) for other in consumed):
            return
        value = match.group("v")
        components["unit"] = value
        consumed.append(span)
        evidence.append(f"unidad '{value}' pegada a la altura (sin etiqueta)")

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
        # Un resto CJK es el ward/municipio (大田区), no un barrio latino.
        if len(rest) == 1 and has_cjk(rest[0]) and "city" in slots:
            slots = ["city"] + [s for s in slots if s != "city"]
        for name, value in zip(slots, rest):
            components[name] = value
            evidence.append(f"{name} '{value}' (segmento posicional)")


def _segment_is_locality(segment: str) -> bool:
    """True si el segmento comma-separated es ciudad/pais/CP, no parte de calle."""
    cleaned = (segment or "").strip(" .,;:-()")
    if not cleaned:
        return True
    folded = fold(cleaned)
    if folded in _locality_blocklist():
        return True
    if re.fullmatch(r"\d{4}", cleaned):
        return True
    if _POSTCODE.fullmatch(cleaned.replace(" ", "")):
        return True
    if _LOCALITY_SEGMENT.search(cleaned):
        return True
    for group in locality_alias_groups():
        if any(folded == fold(item) for item in group):
            return True
    return False


def _segment_is_oa_street_suffix(segment: str) -> bool:
    """Titulo o abreviatura de tipo de via dentro del nombre OA, no localidad."""
    cleaned = (segment or "").strip(" .,;:-()")
    if not cleaned:
        return False
    return bool(_OA_STREET_SUFFIX.match(cleaned))


def _segment_already_has_housenumber(segment: str) -> bool:
    """'Av. Pueyrredon 359' ya es calle+altura; no es un token de nombre OA."""
    cleaned = (segment or "").strip()
    if not cleaned:
        return False
    return bool(
        _ROAD_THEN_NUMBER.search(cleaned)
        or _NUMBERED_STREET.search(cleaned)
        or _EN_NUMBERED_THEN_HOUSE.search(cleaned)
        or _HOUSE_THEN_EN_NUMBERED.search(cleaned)
    )


def _segment_is_admin_locality(segment: str) -> bool:
    """Ciudad/pais/CP en compuestos OA — no barrios de una palabra (Flores, Palermo).

    En 'FLORES, VENANCIO, Gral., 185' el apellido FLORES no es el barrio.
    """
    cleaned = (segment or "").strip(" .,;:-()")
    if not cleaned:
        return True
    if _segment_is_oa_street_suffix(cleaned):
        return False
    if fold(cleaned) in name_glue_words():
        return False
    if re.fullmatch(r"\d{4}", cleaned):
        return True
    if _POSTCODE.fullmatch(cleaned.replace(" ", "")):
        return True
    if _LOCALITY_SEGMENT.search(cleaned):
        return True
    folded = fold(cleaned)
    if len(cleaned) == 2 and cleaned.isalpha():
        return True
    if len(cleaned.split()) >= 2:
        for group in locality_alias_groups():
            if any(folded == fold(item) for item in group):
                return True
    return False


def _join_oa_road_parts(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(p.strip(" .,;:-") for p in parts)).strip()


def _span_covering(raw: str, *fragments: str) -> tuple[int, int] | None:
    """(begin, end) en raw que cubre todos los fragmentos en orden."""
    if not fragments:
        return None
    start = raw.find(fragments[0])
    if start < 0:
        return None
    end = start + len(fragments[0])
    for frag in fragments[1:]:
        idx = raw.find(frag, end)
        if idx < 0:
            return None
        end = idx + len(frag)
    return start, end


def _try_oa_compound_street(raw: str, locales: tuple[str, ...] | None
                            ) -> tuple[int, int, str, str, str] | None:
    """Nombre de via OA con comas internas + altura al final o al inicio.

    'CALDERON DE LA BARCA, PEDRO, 3645' → road='CALDERON DE LA BARCA PEDRO', num=3645
    '3645, CALDERON DE LA BARCA, PEDRO' → idem
    '185 FLORES, VENANCIO, Gral.' → road='FLORES VENANCIO Gral.', num=185
    """
    text = (raw or "").strip()
    if not text or "," not in text:
        return None

    parts = [p.strip() for p in _SPLIT.split(text) if p.strip()]
    if len(parts) < 2:
        return None

    # 'JAMAICA AVE, 108-15, 11418' / 'BROADWAY, 366, 11211' — calle, altura, ZIP.
    # El compuesto CABA toma el ultimo numero como altura; un CP de 5-6 digitos
    # detras de otra altura no es 'PEDRO, 3645'.
    def _street_house_zip() -> bool:
        if len(parts) < 3:
            return False
        tail = parts[-1].replace(" ", "")
        if not _POSTCODE.fullmatch(tail):
            return False
        return any(_HOUSE_ONLY_SEGMENT.match(p) for p in parts[:-1])

    # Altura al final: '…, …, 3645' (>=2 segmentos de nombre antes del numero)
    if _HOUSE_ONLY_SEGMENT.match(parts[-1]) and not _street_house_zip():
        name_parts = parts[:-1]
        number = parts[-1]
        if (len(name_parts) >= 2
                and not any(_segment_already_has_housenumber(p) for p in name_parts)
                and not any(_segment_is_admin_locality(p) for p in name_parts)):
            road = _join_oa_road_parts(name_parts)
            if road and not road_is_suspicious(road, locales):
                span = _span_covering(text, *name_parts, number)
                if span:
                    return (*span, road, number, "oa-compound-trailing")

    # Altura al inicio: '3645, SURNAME, GIVEN' (primera coma tras el numero)
    if _HOUSE_ONLY_SEGMENT.match(parts[0]) and len(parts) >= 3:
        number = parts[0]
        name_parts = parts[1:]
        if (not any(_segment_already_has_housenumber(p) for p in name_parts)
                and not any(_segment_is_admin_locality(p) for p in name_parts)):
            road = _join_oa_road_parts(name_parts)
            if road and not road_is_suspicious(road, locales):
                span = _span_covering(text, number, *name_parts)
                if span:
                    return (*span, road, number, "oa-compound-leading-comma")

    # '185 FLORES, VENANCIO, Gral.' — altura pegada al 1er segmento, >=3 partes de calle
    lead = _OA_LEADING_NUMBER_COMMA.match(text)
    if lead and "," in lead.group("tail"):
        number = lead.group("num").strip()
        tail_parts = [p.strip() for p in _SPLIT.split(lead.group("tail")) if p.strip()]
        if (len(tail_parts) >= 3
                and not any(_segment_already_has_housenumber(p) for p in tail_parts)
                and not any(_segment_is_admin_locality(p) for p in tail_parts)):
            road = _join_oa_road_parts(tail_parts)
            if road and not road_is_suspicious(road, locales):
                span = _span_covering(text, number, *tail_parts)
                if span:
                    return (*span, road, number, "oa-compound-leading")

    return None


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
