"""Maximizar señal geografica en ``address`` para el geocoder.

Esto es NORMALIZACION, no extraccion:
  * componer ``address`` desde partes mapeadas (calle + altura + localidad…)
  * completar provincia/pais a partir de pistas en el texto

Los diccionarios viven en `resources/locality_expand.json` (+ ISO en
`iso3166_alpha2.json`).
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Mapping

from ..resources import (
    LocalityExpansion,
    fold,
    geonames_cue_countries,
    label_set,
    locality_alias_groups,
    locality_expansion_rows,
    phone_region_country_map,
)

_CUE_MIN_LEN = 3

#: orden de localidad al componer el string visible / query
_LOCALITY_PART_KEYS = ("zone", "city", "region", "postcode", "country")

#: 'NE 1st Ave' / '1st Ave' / 'E 65 ST' / '68 ST' — en US la altura va DELANTE.
_US_STYLE_ROAD = re.compile(
    r"^(?:[NS][EW]\.?|[NS]\.?|[EW]\.?)\s+\d{1,3}(?:st|nd|rd|th)\b"
    r"|^\d{1,3}(?:st|nd|rd|th)\b"
    r"|^(?:(?:bch|beach|plumb)\s+)?(?:[NS][EW]\.?|[NS]\.?|[EW]\.)?\s*\d{1,3}\s+"
    r"(?:st|street|ave|avenue|rd|road|pl|place|ct|court|blvd|dr|ln|pkwy|expy)\b",
    re.IGNORECASE,
)


def _alias_keys(folded: str) -> frozenset[str]:
    """Claves de equivalencia para un segmento (el propio fold + grupo de alias)."""
    keys = {folded}
    for group in locality_alias_groups():
        if folded in group:
            keys |= set(group)
    return frozenset(keys)


def already_present(haystack: str, needle: str) -> bool:
    """True si needle (o un alias) ya figura como token, no como substring.

    'Argentina' no está en 'Avenida Patricias Argentinas': si usáramos
    ``n in h`` el país se omitiría o un test leería el nombre de la calle.
    """
    h, n = fold(haystack), fold(needle)
    if not n:
        return True
    return any(_token_in(h, key) for key in ({n} | set(_alias_keys(n))) if key)


def _token_in(haystack: str, token: str) -> bool:
    """``token`` como palabra completa en haystack ya foldeado."""
    return bool(re.search(rf"(?<!\w){re.escape(token)}(?!\w)", haystack))


def dedupe_address_segments(address: str) -> str:
    """Colapsa segmentos repetidos / alias: 'CABA, CABA' → 'CABA'."""
    raw = re.sub(r"\s+", " ", (address or "").strip()).strip(" ,;")
    if not raw or "," not in raw:
        return raw

    parts = [p.strip(" ,;") for p in raw.split(",") if p.strip(" ,;")]
    if len(parts) <= 1:
        return raw

    kept: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = fold(part)
        if not key:
            continue
        aliases = _alias_keys(key)
        if aliases & seen:
            continue
        seen |= aliases
        seen.add(key)
        kept.append(part)

    # Schema: zone → city → region → postcode → country. El país siempre último
    # ('…, Argentina, CABA' → '…, CABA, Argentina') aunque se haya anexado después.
    countries = [p for p in kept if _looks_like_country(p)]
    if countries:
        kept = [p for p in kept if not _looks_like_country(p)] + countries
    return ", ".join(kept)


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _str_part(value: Any) -> str:
    if _blank(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip()).strip(" ,;")


def house_number_already_in_street(street: str, number: str) -> bool:
    """True si la altura ya figura como token en la calle ('Av. Nazca 400')."""
    s, n = (street or "").strip(), (number or "").strip()
    if not s or not n:
        return False
    # token exacto (evita que '4' matchee '400' / '14')
    return bool(re.search(rf"(?<!\d){re.escape(n)}(?!\d)", s, flags=re.IGNORECASE))


def compose_address_from_parts(values: Mapping[str, Any]) -> str | None:
    """Arma address final desde partes del schema.

    Cualquier formato (CSV/JSON/XLSX) que mapee a house_number / city / …
    termina en el mismo string. No inventa calle si no hay ``address``.
    """
    street = _str_part(values.get("address"))
    number = _str_part(values.get("house_number"))
    unit = _str_part(values.get("unit"))
    locality = [_str_part(values.get(k)) for k in _LOCALITY_PART_KEYS]
    locality = [p for p in locality if p]

    if not street and not number and not locality:
        return None
    if not street:
        # Sin calle no fabricamos un "address" solo con ciudad/CP: no es geocodable
        # como puerta y confundiria al gate / UI.
        return None

    if number and not house_number_already_in_street(street, number):
        country = _str_part(values.get("country"))
        us = iso_from_country_label(country) == "US" or bool(_US_STYLE_ROAD.match(street))
        core = (f"{number} {street}" if us else f"{street} {number}").strip()
    else:
        core = street

    if unit and not already_present(core, unit):
        # "Piso 3" / "Depto B" — no confundir con altura
        core = f"{core}, {unit}"

    extras = [p for p in locality if not already_present(core, p)]
    if extras:
        core = f"{core}, {', '.join(extras)}"
    return dedupe_address_segments(core) or None


def apply_composed_address(values: dict[str, Any]) -> bool:
    """Escribe ``address`` compuesto si aporta mas que lo mapeado. True si cambio."""
    composed = compose_address_from_parts(values)
    if not composed:
        return False
    current = _str_part(values.get("address"))
    if fold(composed) == fold(current):
        return False
    # Solo reemplazar si el compuesto es mas rico (mas largo tras fold) o current vacio
    if current and len(fold(composed)) < len(fold(current)):
        return False
    values["address"] = composed
    return True


def _haystack_tokens(folded: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", folded))


def _cue_matches(folded_haystack: str, cue: str, tokens: frozenset[str] | None = None) -> bool:
    """Match de localidad por token, no substring ('ne' no pega en 'biscayne')."""
    c = fold(cue)
    if len(c) < _CUE_MIN_LEN:
        return False
    if " " in c or "-" in c:
        return f" {c} " in f" {folded_haystack} "
    bag = tokens if tokens is not None else _haystack_tokens(folded_haystack)
    return c in bag


#: (filas indexadas, token → filas, filas sin token ASCII que se prueban siempre)
_cue_index_cache: tuple[tuple[LocalityExpansion, ...], dict[str, tuple[int, ...]],
                        tuple[int, ...]] | None = None


def _candidate_rows(tokens: frozenset[str]) -> list[LocalityExpansion]:
    """Las filas cuya pista PUEDE aparecer en el texto, en el orden original.

    Recorrer las ~32 mil filas re-foldeando cada pista costaba 35-50 ms por
    direccion. `_cue_matches` solo acepta una pista suelta si esta en la bolsa de
    tokens, y una frase si su primer token esta; asi que indexar por ese token no
    deja afuera ninguna fila que la recorrida completa hubiera aceptado. Las
    frases sin token ASCII ('санкт-петербург') no se pueden indexar y se prueban
    siempre. Lo que decide sigue siendo `_expansion_applies`, fila por fila.
    """
    global _cue_index_cache
    rows = locality_expansion_rows()
    if _cue_index_cache is None or _cue_index_cache[0] is not rows:
        by_token: dict[str, list[int]] = {}
        always: list[int] = []
        for position, row in enumerate(rows):
            for cue in row.cues:
                if len(cue) < _CUE_MIN_LEN:
                    continue
                if " " in cue or "-" in cue:
                    first = re.search(r"[a-z0-9]+", cue)
                    if first is None:
                        always.append(position)
                        continue
                    key = first.group(0)
                else:
                    key = cue
                by_token.setdefault(key, []).append(position)
        _cue_index_cache = (rows, {k: tuple(v) for k, v in by_token.items()},
                            tuple(always))
    _, by_token, always = _cue_index_cache
    positions = set(always)
    for token in tokens:
        positions.update(by_token.get(token, ()))
    return [rows[p] for p in sorted(positions)]


#: separadores de segmento dentro de una direccion escrita
_SEGMENTS = re.compile(r"[,;|]|\s+-\s+")
#: una altura de calle: numero pelado, con a lo sumo una letra ('1234', '450b').
#: Un CPA ('b7000') o un codigo postal largo NO cuentan.
_BARE_NUMBER = re.compile(r"^\d{1,5}[a-z]?$")


def _cue_is_street(folded_haystack: str, cue: str) -> bool:
    """True si la pista de localidad esta pegada a una altura: es una calle.

    Media ciudad del mundo comparte nombre con una calle de otra: Callao es un
    puerto peruano y una avenida porteña, Asuncion es la capital de Paraguay y
    una calle de Buenos Aires, Cordoba es las dos cosas en el mismo pais. El
    gazetteer no puede distinguirlas, pero la posicion si: nadie escribe la
    ciudad pegada a un numero de puerta.

        'callao 2862'        -> calle   (la altura viene inmediatamente despues)
        'b7000 tandil'       -> ciudad  (b7000 es un CPA, no una altura)
        'downtown miami'     -> ciudad
    """
    c = fold(cue)
    largo = len(c.split())
    for segment in _SEGMENTS.split(folded_haystack):
        words = segment.split()
        for i in range(len(words) - largo + 1):
            if " ".join(words[i:i + largo]) != c:
                continue
            antes = words[i - 1] if i > 0 else ""
            despues = words[i + largo] if i + largo < len(words) else ""
            if _BARE_NUMBER.match(antes) or _BARE_NUMBER.match(despues):
                return True
    return False


#: palabras de un segmento foldeado, sin la puntuacion
_WORD = re.compile(r"[^\W_]+")

#: donde esta una pista de GeoNames, de menos a mas evidencia de localidad
_INSIDE, _AFTER_HOUSE_NUMBER, _OWN_SEGMENT = 0, 1, 2


def _is_locality_decoration(word: str) -> bool:
    """Lo que acompaña a una ciudad sin dejar de ser su segmento: CP, CPA o sigla.

    'b7000 tandil', 'campinas sp 13010', 'springfield il 62701'. Un tipo de via de
    dos letras no es sigla: 'elgin rd 114' es una calle, no Elgin (Illinois).
    """
    if any(ch.isdigit() for ch in word):
        return True
    return len(word) == 2 and word.isalpha() and word not in label_set("street_tokens", None)


def _geonames_cue_position(folded_haystack: str, cue: str) -> int:
    """Que tan en posicion de localidad esta una pista de GeoNames.

    GeoNames trae 32 mil ciudades y muchas se llaman como un apellido o una calle
    de otro pais: Lopez (Filipinas), Castro (Brasil), Medina (Irak), Cabildo
    (Chile). Que la palabra aparezca no dice nada; donde aparece, si:

        'gorriti 4500, 3b, lopez'   -> _OWN_SEGMENT         su propio segmento
        '123 main st springfield'   -> _AFTER_HOUSE_NUMBER  cierra la direccion
        'juan lopez gorriti 4500'   -> _INSIDE              nombre o calle

    El primer segmento es la calle (igual que en `geocoding.scoring`). Una pista
    pegada a una altura ('callao 1219', '4500 lopez') es calle en cualquier lado.
    """
    target = _WORD.findall(cue)
    if not target:
        return _INSIDE
    best = _INSIDE
    for index, segment in enumerate(_SEGMENTS.split(folded_haystack)):
        words = _WORD.findall(segment)
        for i in range(len(words) - len(target) + 1):
            if words[i:i + len(target)] != target:
                continue
            before, after = words[:i], words[i + len(target):]
            if ((before and _BARE_NUMBER.match(before[-1]))
                    or (after and _BARE_NUMBER.match(after[0]))):
                continue
            if not all(_is_locality_decoration(w) for w in after):
                continue
            # Adelante solo un CP ('b7000 tandil'): una sigla adelante es un
            # tratamiento, y 'sr lopez' es una persona.
            if index > 0 and all(any(ch.isdigit() for ch in w) for w in before):
                return _OWN_SEGMENT
            if any(_BARE_NUMBER.match(w) for w in before):
                best = _AFTER_HOUSE_NUMBER
    return best


def _expansion_applies(row: LocalityExpansion, folded_haystack: str,
                       tokens: frozenset[str], explicit_iso: str | None,
                       written_iso: str | None = None) -> bool:
    matched = [cue for cue in row.cues if _cue_matches(folded_haystack, cue, tokens)]
    if not matched:
        return False
    row_isos = {iso for token in row.tokens if (iso := _country_iso(token))}
    # Un pais escrito en la direccion le gana a cualquier pista:
    # 'guatemala, Bulevar Ensenada de San Isidro 13-55' no es Peru.
    if written_iso and row_isos and written_iso not in row_isos:
        return False
    # Tambien coincide si el texto ya nombra el pais de la fila, aunque sea sin
    # coma: 'San Francisco united states SANSOME ST 1045'.
    agrees = (explicit_iso is None or not row_isos or explicit_iso in row_isos
              or any(_country_iso(t) and already_present(folded_haystack, t)
                     for t in row.tokens))

    if row.curated:
        # Una pista pegada a una altura es el nombre de la CALLE, no la ciudad.
        # Sin esto 'Av Callao 1219' se enriquece con 'Peru' y la query sale a
        # buscar la direccion al pais equivocado.
        places = [cue for cue in matched if not _cue_is_street(folded_haystack, cue)]
        # Tampoco pisa al pais del depot/telefono si ese pais tiene una ciudad
        # con el mismo nombre: 'San Isidro' con AR es el partido, no Lima; 'Paris'
        # con US es Texas.
        return bool(places) and (
            agrees or any(not _city_in_country(cue, explicit_iso) for cue in places))

    # GeoNames: 'Juan Lopez 1133334444 Gorriti 4500' dejaba 'Juan Lopez Gorriti
    # 4500, Philippines' y el geocoder vetaba los candidatos argentinos.
    for cue in matched:
        if cue in _country_name_keys():
            continue    # 'Mexico' tambien es un pueblo de Filipinas; el segmento es el pais
        position = _geonames_cue_position(folded_haystack, cue)
        # En su segmento pisa al pais del depot/telefono, salvo que ese pais tenga
        # una ciudad con el mismo nombre: 'Av Massey 100, Lincoln' con AR es
        # Lincoln (Buenos Aires), no Nebraska.
        if position == _OWN_SEGMENT and (agrees or not _city_in_country(cue, explicit_iso)):
            return True
        # Cerrando la direccion sin coma es evidencia debil: completa el pais si
        # nadie lo dijo, pero no pisa el del depot ni el del telefono.
        if position == _AFTER_HOUSE_NUMBER and agrees:
            return True
    return False


def _city_in_country(cue: str, iso: str | None) -> bool:
    return any(_country_iso(name) == iso for name in geonames_cue_countries().get(cue, ()))


@lru_cache(maxsize=1)
def _country_name_keys() -> frozenset[str]:
    """Nombres de pais foldeados: los de la tabla ISO, los preferidos y los alias."""
    return frozenset(_iso_by_country_name()) | frozenset(_country_names())


@lru_cache(maxsize=1)
def _iso_by_country_name() -> dict[str, str]:
    """Nombre foldeado → ISO-2, con los nombres de GeoNames ('Brazil') y los preferidos ('Brasil')."""
    from ..resources import load_json
    table = {fold(str(name)): str(code).upper()
             for code, name in load_json("iso3166_alpha2.json").items()}
    table.update({fold(name): code for code, name in phone_region_country_map().items()})
    return table


def _country_iso(label: str) -> str | None:
    """Nombre exacto de pais → ISO-2. Sin substring: 'y Portugal' es una calle de Quito."""
    return _iso_by_country_name().get(fold(label))


@lru_cache(maxsize=1)
def _country_names() -> frozenset[str]:
    names = {fold(v) for v in phone_region_country_map().values()}
    names.update({
        "united states", "usa", "estados unidos", "estados unidos de america",
        "uk", "great britain", "england",
    })
    return frozenset(names)


def _looks_like_country(token: str) -> bool:
    raw = (token or "").strip()
    if not raw:
        return False
    if fold(raw) in _country_names():
        return True
    if len(raw) == 2 and raw.isalpha():
        return raw.upper() in phone_region_country_map()
    return False


def infer_locality_tokens(text: str) -> list[str]:
    """Ciudad/pais del documento (p.ej. header 'Deliveries for today (Miami)').

    Solo el JSON curado: GeoNames es demasiado amplio para un header.
    """
    from ..resources import load_json
    folded = fold(text)
    if not folded:
        return []
    bag = _haystack_tokens(folded)
    curated = load_json("locality_expand.json").get("expansions") or ()
    for row in curated:
        cues = row.get("cues") or ()
        tokens = row.get("tokens") or ()
        hit = next((c for c in cues if _cue_matches(folded, c, bag)), None)
        if not hit:
            continue
        out: list[str] = []
        label = hit.strip()
        if label and not already_present(" ".join(tokens), label):
            out.append(label.title() if label.islower() else label)
        for tok in tokens:
            if tok not in out:
                out.append(tok)
        return out
    return []


def country_from_phone_region(phone_region: str | None) -> str | None:
    if not phone_region:
        return None
    country = phone_region_country_map().get(phone_region.upper())
    if country:
        return country
    from ..resources import load_json
    return load_json("iso3166_alpha2.json").get(phone_region.casefold())


def iso_from_country_label(label: str | None) -> str | None:
    """'United States' / 'US' / 'Estados Unidos…' → ISO-2."""
    raw = (label or "").strip()
    if not raw:
        return None
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    folded = fold(raw)
    aliases = {
        "united states": "US", "usa": "US", "estados unidos": "US",
        "estados unidos de america": "US",
        "argentina": "AR", "brasil": "BR", "brazil": "BR",
    }
    if folded in aliases:
        return aliases[folded]
    for iso, name in phone_region_country_map().items():
        if fold(name) == folded or fold(name) in folded:
            return iso
    return None


def maximize_address_for_geocode(
    address: str,
    *,
    phone_region: str | None = None,
    extra_tokens: tuple[str, ...] | list[str] | None = None,
    country: str | None = None,
) -> str:
    """Devuelve address + tokens geograficos faltantes; colapsa duplicados.

    El pais del ``phone_region`` NO se agrega si el texto (o extra_tokens) ya
    implica otro pais — un paste de Miami no puede terminar en Argentina
    solo porque RouteHub mando ``phone_region=AR``.
    """
    written = re.sub(r"\s+", " ", (address or "").strip()).strip(" ,;")
    raw = dedupe_address_segments(written)
    if not raw:
        return raw

    extras: list[str] = []
    folded = fold(raw)
    bag = _haystack_tokens(folded)
    written_country = _written_country(written)
    written_iso = iso_from_country_label(written_country) if written_country else None
    explicit_iso = (written_iso or iso_from_country_label(country)
                    or (phone_region or "").upper() or None)
    for row in _candidate_rows(bag):
        if _expansion_applies(row, folded, bag, explicit_iso, written_iso):
            for tok in row.tokens:
                if tok not in extras:
                    extras.append(tok)
            break
    # Los tokens de una fila de expansion son paises por construccion: 'Brazil'
    # tiene que implicar Brasil aunque no este entre los nombres preferidos. Sin
    # esto 'Campinas' salia con 'Brazil, Argentina'.
    expansion_country = next((t for t in extras if _country_iso(t)), None)

    if extra_tokens:
        for tok in extra_tokens:
            t = (tok or "").strip()
            if t and t not in extras:
                extras.append(t)

    # Un pais escrito en la direccion tambien lo implica: 'Carrera 7 45, Colombia'
    # con phone_region=AR no puede terminar en 'Colombia, Argentina'.
    implied_country = (country or written_country or expansion_country
                       or next((t for t in extras if _looks_like_country(t)), None))
    region_country = country_from_phone_region(phone_region)
    if region_country and not implied_country:
        extras.append(region_country)
    # si implied_country != region_country: se ignora phone_region (conflicto)

    if country and country not in extras:
        extras.append(country)

    missing = _missing_tokens(raw, extras)
    if not missing:
        return dedupe_address_segments(raw)
    return dedupe_address_segments(f"{raw}, {', '.join(missing)}")


def _written_country(address: str) -> str | None:
    """El pais que la direccion ya nombra en un segmento propio, si lo hay.

    Solo nombres de pais preferidos o sus alias ('Colombia', 'USA'), no la tabla
    ISO entera: 'Georgia' es un estado, 'Chad' y 'Jordan' son nombres. Seguido de
    una altura es la calle: en 'Nicaragua, 4824' (export OA de CABA) Nicaragua es
    la calle; en 'guatemala, Bulevar Ensenada 13-55' es el pais.
    """
    parts = [part.strip(" .") for part in _SEGMENTS.split(address)]
    for index, label in enumerate(parts):
        if not label or fold(label) not in _country_names():
            continue
        following = parts[index + 1] if index + 1 < len(parts) else ""
        if re.fullmatch(r"\d{1,5}\s*[a-z]{0,3}", fold(following)):    # '4824', '6474BIS'
            continue
        return label
    return None


def _missing_tokens(raw: str, extras: list[str]) -> list[str]:
    """Lo que falta agregar, sin repetir un pais con otro nombre ('Czech Republic' y 'Czechia')."""
    countries = {iso for part in _SEGMENTS.split(raw) if (iso := _country_iso(part.strip()))}
    missing: list[str] = []
    for token in extras:
        if already_present(raw, token):
            continue
        iso = _country_iso(token)
        if iso and iso in countries:
            continue
        if iso:
            countries.add(iso)
        missing.append(token)
    return missing
