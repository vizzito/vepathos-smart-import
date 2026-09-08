"""Maximizar señal geografica en ``address`` para el geocoder.

Esto es NORMALIZACION, no extraccion:
  * componer ``address`` desde partes mapeadas (calle + altura + localidad…)
  * completar provincia/pais a partir de pistas en el texto

Los diccionarios viven en `resources/locality_expand.json` (+ ISO en
`iso3166_alpha2.json`).
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from ..resources import (
    fold,
    locality_alias_groups,
    locality_expansions,
    phone_region_country_map,
)

_CUE_MIN_LEN = 3

#: orden de localidad al componer el string visible / query
_LOCALITY_PART_KEYS = ("zone", "city", "region", "postcode", "country")

#: 'NE 1st Ave' / '1st Ave' — en US la altura va DELANTE; al revés el parser
#: come el ordinal ('1st' → house=1) y no geocodifica.
_US_STYLE_ROAD = re.compile(
    r"^(?:[NS][EW]\.?|[NS]\.?|[EW]\.?)\s+\d{1,3}(?:st|nd|rd|th)\b"
    r"|^\d{1,3}(?:st|nd|rd|th)\b",
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


def _country_names() -> set[str]:
    names = {fold(v) for v in phone_region_country_map().values()}
    names.update({
        "united states", "usa", "estados unidos", "estados unidos de america",
        "uk", "great britain", "england",
    })
    return names


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
    raw = dedupe_address_segments(
        re.sub(r"\s+", " ", (address or "").strip()).strip(" ,;")
    )
    if not raw:
        return raw

    extras: list[str] = []
    folded = fold(raw)
    bag = _haystack_tokens(folded)
    for cues, tokens in locality_expansions():
        # Una pista pegada a una altura es el nombre de la CALLE, no la ciudad.
        # Sin esto 'Av Callao 1219' se enriquece con 'Peru' y la query sale a
        # buscar la direccion al pais equivocado.
        if any(_cue_matches(folded, cue, bag) and not _cue_is_street(folded, cue)
               for cue in cues):
            for tok in tokens:
                if tok not in extras:
                    extras.append(tok)
            break

    if extra_tokens:
        for tok in extra_tokens:
            t = (tok or "").strip()
            if t and t not in extras:
                extras.append(t)

    implied_country = country or next((t for t in extras if _looks_like_country(t)), None)
    region_country = country_from_phone_region(phone_region)
    if region_country and not implied_country:
        extras.append(region_country)
    elif region_country and implied_country and fold(region_country) == fold(implied_country):
        pass
    # si implied_country != region_country: se ignora phone_region (conflicto)

    if country and country not in extras:
        extras.append(country)

    missing = [t for t in extras if not already_present(raw, t)]
    if not missing:
        return dedupe_address_segments(raw)
    return dedupe_address_segments(f"{raw}, {', '.join(missing)}")
