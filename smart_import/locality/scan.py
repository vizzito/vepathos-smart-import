"""De qué ciudad habla el archivo (no de qué ciudad es el depot).

El usuario puede tener seleccionado un depot en CABA y subir un paste de Miami.
Geocodificar con la región equivocada no devuelve pines malos: no devuelve
NINGUNO, porque el índice OSM que se abre es de otro país.

Esta capa **no decide nada**. Junta evidencia y la publica en el report para
que la UI pregunte, y para que el geocode reciba una ciudad ya confirmada por
el usuario en `depot_city`. Lo que hoy manda el selector se sigue respetando.

Fuentes, de más fuerte a más débil:

  1. columnas normalizadas city / region / postcode / country  (moda de filas)
  2. cola de las direcciones: "350 NE 1st Ave, Brickell, Miami, FL"
  3. encabezado del documento: "Deliveries for today (Miami)"

Asimetría a propósito: una ciudad que viene de una COLUMNA se acepta tal cual
(puede ser un pueblo de 3.000 habitantes que GeoNames no lista). Una ciudad
ADIVINADA del texto solo se propone si GeoNames la conoce — sin ese filtro,
"Entrega" y "Despacho" entrarían como localidad.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Mapping

#: A partir de acá la sugerencia es fuerte (la UI igual confirma).
ACCEPT_CONFIDENCE = 0.80
#: Debajo de acá no vale ni como sugerencia: la UI pide la ciudad.
SUGGEST_CONFIDENCE = 0.55
#: Dos candidatos más cerca que esto = ambiguo, hay que preguntar.
AMBIGUOUS_MARGIN = 0.15
#: Filas que tienen que respaldar una ciudad para que "todas coinciden" valga.
#: Sin esto, un archivo de UNA fila da ratio 1.0 y la sugerencia sale con la
#: misma fuerza que 55 filas de acuerdo.
MIN_STRONG_SUPPORT = 3

#: Líneas de encabezado que se miran antes del primer ítem de la lista.
HEADER_LINES = 6
#: "Ciudad Autónoma de Buenos Aires" entra; una oración no.
MAX_LABEL_WORDS = 5

#: "City: NYC", "Zona: Tandil", "Ciudad = Miami"
_HEADER_HINT = re.compile(
    r"\b(?:city|ciudad|localidad|zona|zone|region|regi[oó]n|provincia|state|"
    r"depot|dep[oó]sito)\b\s*[:=]\s*(?P<value>[^,;|\n]{2,60})",
    re.IGNORECASE,
)
#: "Deliveries for today (Miami)" / "[Tandil]"
_PARENTHESIZED = re.compile(r"[(\[]\s*(?P<value>[^)\]]{2,60})\s*[)\]]")
#: Primer ítem de la lista: ahí termina el encabezado (igual que el segmenter).
_LIST_ITEM = re.compile(r"^\s*(?:\d{1,3}[).\-:]|[-*•·–—])\s+")


@dataclass(frozen=True)
class LocalityCandidate:
    """Una localidad posible, con de dónde salió y cuánto la respalda."""
    city: str | None = None
    region: str | None = None
    postcode: str | None = None
    country: str | None = None
    iso: str | None = None
    lat: float | None = None
    lon: float | None = None
    confidence: float = 0.0
    #: filas que mencionan esta localidad
    support: int = 0
    #: "rows" | "address" | "header"
    sources: tuple[str, ...] = ()
    #: ISO de países donde existe el mismo nombre (Córdoba: AR y ES)
    homonym_countries: tuple[str, ...] = ()

    @property
    def is_homonym(self) -> bool:
        return len(self.homonym_countries) > 1

    def as_dict(self) -> dict:
        return {
            "city": self.city, "region": self.region, "postcode": self.postcode,
            "country": self.country, "iso": self.iso,
            "lat": self.lat, "lon": self.lon,
            "confidence": self.confidence, "support": self.support,
            "sources": list(self.sources),
            "homonym_countries": list(self.homonym_countries),
        }


@dataclass(frozen=True)
class LocalityEvidence:
    """Lo que el archivo dice sobre dónde entrega, sin decidir nada."""
    candidates: tuple[LocalityCandidate, ...] = ()
    rows_total: int = 0
    country: str | None = None

    @property
    def best(self) -> LocalityCandidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def ambiguous(self) -> bool:
        """Dos ciudades distintas empatadas, o un homónimo sin país que lo corte."""
        best = self.best
        if best is None:
            return False
        if best.is_homonym:
            return True
        if len(self.candidates) < 2:
            return False
        second = self.candidates[1]
        return best.confidence - second.confidence < AMBIGUOUS_MARGIN

    @property
    def needs_user_input(self) -> bool:
        """True = la UI tiene que preguntar la ciudad antes de geocodificar.

        Debajo de `SUGGEST_CONFIDENCE` no hay ni sugerencia que mostrar. Entre
        SUGGEST y ACCEPT hay una sugerencia, pero se confirma: geocodificar con
        la region equivocada no da pines malos, da CERO pines.
        """
        best = self.best
        if best is None or best.confidence < ACCEPT_CONFIDENCE:
            return True
        return self.ambiguous

    @property
    def reason(self) -> str:
        best = self.best
        if best is None:
            return "no se detecto ninguna ciudad en el archivo"
        if best.is_homonym:
            return (f"'{best.city}' existe en "
                    f"{', '.join(best.homonym_countries)}: falta el pais")
        if self.ambiguous:
            return "hay mas de una ciudad posible en el archivo"
        if best.confidence < SUGGEST_CONFIDENCE:
            return "la evidencia de ciudad es debil"
        if best.confidence < ACCEPT_CONFIDENCE:
            return "conviene confirmar la ciudad"
        return ""

    def as_dict(self) -> dict:
        return {
            "best": self.best.as_dict() if self.best else None,
            "candidates": [c.as_dict() for c in self.candidates],
            "rows_total": self.rows_total,
            "country": self.country,
            "needs_user_input": self.needs_user_input,
            "ambiguous": self.ambiguous,
            "reason": self.reason,
        }


def _clean(value: Any) -> str | None:
    """Etiqueta de localidad usable, o None.

    Reusa `normalize_locality_label` (descarta CP sueltos y 'Comuna 2', expande
    ISO-2 de país) y además corta lo que no puede ser un nombre de localidad:
    frases largas y cualquier cosa con dígitos.
    """
    from ..geocoding.depot_context import normalize_locality_label

    if value is None:
        return None
    label = normalize_locality_label(str(value))
    if not label:
        return None
    label = re.sub(r"\s+", " ", label).strip(" ,;.|-")
    if not label or re.search(r"\d", label):
        return None
    if len(label.split()) > MAX_LABEL_WORDS:
        return None
    return label


def _postcode(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _tail_labels(address: str) -> list[str]:
    """Segmentos de la dirección después de la calle: barrio, ciudad, estado."""
    parts = [p.strip() for p in (address or "").split(",")]
    return [p for p in parts[1:] if p]


def _header_lines(document: str) -> list[str]:
    """Líneas de encabezado: hasta el primer ítem de lista, máximo HEADER_LINES."""
    out: list[str] = []
    for raw in (document or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _LIST_ITEM.match(line):
            break
        out.append(line)
        if len(out) >= HEADER_LINES:
            break
    return out


def _header_labels(document: str) -> list[str]:
    """Candidatos del encabezado: '(Miami)', 'City: NYC', 'Entregas Tandil'."""
    out: list[str] = []
    for line in _header_lines(document):
        for pattern in (_HEADER_HINT, _PARENTHESIZED):
            for match in pattern.finditer(line):
                for part in match.group("value").split(","):
                    part = part.strip()
                    if part:
                        out.append(part)
        # El resto de la línea, por comas: 'Entregas de hoy, Tandil'
        for part in line.split(","):
            part = part.strip(" .:;|-")
            if part and len(part.split()) <= MAX_LABEL_WORDS:
                out.append(part)
    return out


@lru_cache(maxsize=1)
def _prose_stopwords() -> frozenset[str]:
    """Palabras del lexicón que NO pueden ser una ciudad leída de la prosa.

    GeoNames sola no alcanza: 'Hola' es una ciudad de Kenia, así que un paste
    que arranca con "Hola" detectaría Kenia. El filtro se aplica SOLO al
    encabezado, porque 'Mobile', 'Largo', 'Alameda' y 'Esquina' también son
    etiquetas del lexicón y son ciudades reales: si vienen de una columna o de
    la cola de una dirección, se respetan.
    """
    from ..resources import LABEL_GROUPS, label_set

    words: set[str] = set()
    for group in LABEL_GROUPS:
        words |= set(label_set(group))
    return frozenset(words)


def _new_bucket() -> dict:
    return {
        "labels": Counter(), "support": 0, "sources": set(),
        "regions": Counter(), "postcodes": Counter(), "countries": Counter(),
    }


def detect_locality(
    rows: Iterable[Mapping[str, Any]] | None = None,
    document: str | None = None,
) -> LocalityEvidence:
    """Junta la evidencia de localidad del archivo ya normalizado.

    `rows` son los valores de las filas normalizadas (city / region / postcode /
    country / address). `document` es el texto original cuando el archivo era
    texto libre — de ahí sale el encabezado.
    """
    from ..geocoding.city_lookup import city_matches
    from ..normalization.address import (
        country_from_phone_region, iso_from_country_label,
    )
    from ..resources import fold

    row_list = [r for r in (rows or []) if r]
    buckets: dict[str, dict] = {}
    countries: Counter[str] = Counter()
    regions_global: Counter[str] = Counter()

    def bucket_for(label: str) -> dict:
        key = fold(label)
        bucket = buckets.setdefault(key, _new_bucket())
        bucket["labels"][label] += 1
        return bucket

    # 1) Columnas normalizadas: la fuente que no hay que adivinar.
    for row in row_list:
        country = _clean(row.get("country"))
        if country:
            countries[country] += 1
        region = _clean(row.get("region"))
        if region:
            regions_global[region] += 1
        city = _clean(row.get("city"))
        if not city:
            continue
        bucket = bucket_for(city)
        bucket["support"] += 1
        bucket["sources"].add("rows")
        if region:
            bucket["regions"][region] += 1
        if country:
            bucket["countries"][country] += 1
        if postcode := _postcode(row.get("postcode")):
            bucket["postcodes"][postcode] += 1

    country_hint = countries.most_common(1)[0][0] if countries else None

    # 2) Cola de las direcciones, solo para las filas sin columna de ciudad.
    #    Se cuenta UNA por fila: '…, Brickell, Miami, FL' no vale doble.
    for row in row_list:
        if _clean(row.get("city")):
            continue
        for label in _tail_labels(str(row.get("address") or "")):
            city = _clean(label)
            if not city or not city_matches(city, country_hint):
                continue
            bucket = bucket_for(city)
            bucket["support"] += 1
            bucket["sources"].add("address")
            if postcode := _postcode(row.get("postcode")):
                bucket["postcodes"][postcode] += 1
            break

    # 3) Encabezado del documento. Acá se exige que GeoNames la conozca Y que
    #    no sea una palabra de saludo / etiqueta ('Hola' es ciudad en Kenia).
    stopwords = _prose_stopwords()
    for label in _header_labels(document or ""):
        city = _clean(label)
        if not city or fold(city) in stopwords:
            continue
        if not city_matches(city, country_hint):
            # Puede no ser ciudad sino el país: 'Entregas Argentina'.
            if iso_from_country_label(city):
                countries[city] += 1
            continue
        bucket_for(city)["sources"].add("header")

    if countries and country_hint is None:
        country_hint = countries.most_common(1)[0][0]

    total = len(row_list)
    candidates: list[LocalityCandidate] = []
    for bucket in buckets.values():
        label = bucket["labels"].most_common(1)[0][0]
        support = bucket["support"]
        sources = tuple(sorted(bucket["sources"]))
        country = (bucket["countries"].most_common(1)[0][0]
                   if bucket["countries"] else country_hint)
        hits = city_matches(label, country)

        confidence = 0.0
        if support and total:
            # Pesa el acuerdo (1 de 100 no es evidencia) Y la cantidad absoluta
            # (1 de 1 tampoco: un token suelto no puede valer como 55 filas).
            agreement = support / total
            volume = min(support / MIN_STRONG_SUPPORT, 1.0)
            confidence = 0.45 + 0.45 * agreement * volume
        elif "header" in sources:
            # Solo el encabezado: alcanza para pre-cargar el selector, no para
            # geocodificar sin preguntar.
            confidence = 0.55
        if support and "header" in sources:
            confidence += 0.10
        if hits:
            confidence += 0.05

        isos = tuple(sorted({h.iso for h in hits}))
        if len(isos) > 1:
            # Homónimo sin país que lo corte: no se elige, se pregunta.
            confidence *= 0.85
        if support == 0 and not hits:
            continue

        iso = (hits[0].iso if len(isos) == 1 else None) or iso_from_country_label(country)
        if country is None and iso:
            # El pais que confirmo GeoNames sirve para enriquecer la query
            # ('Miami' -> 'Miami, United States'); sin esto queda en None.
            country = country_from_phone_region(iso)

        region = (bucket["regions"].most_common(1)[0][0] if bucket["regions"]
                  else (regions_global.most_common(1)[0][0] if regions_global else None))
        postcode = (bucket["postcodes"].most_common(1)[0][0]
                    if bucket["postcodes"] else None)
        candidates.append(LocalityCandidate(
            city=label,
            region=region,
            postcode=postcode,
            country=country,
            iso=iso,
            lat=hits[0].lat if hits and len(isos) == 1 else None,
            lon=hits[0].lon if hits and len(isos) == 1 else None,
            confidence=round(min(confidence, 0.98), 3),
            support=support,
            sources=sources,
            homonym_countries=isos if len(isos) > 1 else (),
        ))

    candidates.sort(key=lambda c: (-c.confidence, -c.support, c.city or ""))
    return LocalityEvidence(
        candidates=tuple(candidates[:5]),
        rows_total=total,
        country=country_hint,
    )
