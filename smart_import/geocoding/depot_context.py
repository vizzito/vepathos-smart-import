"""Contexto geografico del depot: enriquecimiento de queries y geofence.

Diseno mundial (sin bboxes ni ciudades hardcodeadas):

  * Los tokens de enrichment salen SOLO de campos estructurados
    (city / region / country / postcode explicito).
  * La localidad se completa con el indice OSM local
    (`fill_depot_from_index`) alrededor de lat/lon — eso ya es global.
  * Nunca se parte el reverse-geocode libre del depot (trae calle/barrio
    y ensucia la query de cada entrega).
  * ISO-3166 alpha-2 → nombre es dato estandar, no logica de negocio regional.
  * Palabras de subdivision administrativa viven en
    `resources/geo_keywords.json` (`admin_unit_words`).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..normalization.address import already_present, dedupe_address_segments
from ..resources import admin_unit_words, fold
from .scoring import haversine_km

# Patrones estructurales (no listas de ciudades):
# - CP genericos mundiales (AR B1234, US 12345, UK-ish, IN 6 digitos, …)
_POSTCODE_ONLY = re.compile(
    r"^(?:[A-Z]?\d{4}[A-Z]{0,3}|\d{4,6}|[A-Z]\d{4}[A-Z]{3})$",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _numbered_admin_re() -> re.Pattern[str]:
    """'Comuna 2', '2nd District', 'Arrondissement 11' — no son ciudad.

    El patron se compila SOBRE TEXTO FOLDEADO y se matchea contra `fold(texto)`:
    los exports reales llegan tanto 'Quận 1' como 'Quan 1', 'ilçe' como 'ilce'.
    Comparar con la forma acentuada solamente deja pasar la mitad de los casos.
    """
    words = "|".join(
        re.escape(w) for w in sorted({fold(w) for w in admin_unit_words()},
                                     key=len, reverse=True)
    )
    return re.compile(
        rf"^(?:(?:{words})\s+(?:n(?:o|um)?\.?\s*)?\d+"
        rf"|\d+(?:st|nd|rd|th)?\s+(?:{words})"
        rf")$",
        re.IGNORECASE,
    )


def _looks_like_postcode_only(text: str) -> bool:
    return bool(_POSTCODE_ONLY.match((text or "").strip()))


@lru_cache(maxsize=1)
def _iso3166_alpha2() -> dict[str, str]:
    """Mapa ISO-3166-1 alpha-2 → nombre ingles (recurso estatico, no regional)."""
    path = Path(__file__).resolve().parents[1] / "resources" / "iso3166_alpha2.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {str(k).casefold(): str(v) for k, v in raw.items()}


def normalize_locality_label(value: str | None) -> str | None:
    """Limpia una etiqueta de localidad sin asumir pais/ciudad concretos.

    - descarta CP sueltos y subdivisiones numeradas ("Comuna 2")
    - expande ISO-2 de pais a nombre (AR → Argentina) via tabla ISO
    - el resto se deja tal cual vino del cliente / OSM
    """
    if not value or not str(value).strip():
        return None
    t = str(value).strip()
    if _looks_like_postcode_only(t) or _numbered_admin_re().match(fold(t)):
        return None
    if len(t) == 2 and t.isalpha():
        return _iso3166_alpha2().get(t.casefold(), t.upper())
    return t


def _already_present(haystack: str, needle: str) -> bool:
    """True si needle (o alias CABA/Ciudad Autónoma…) ya esta en haystack."""
    return already_present(haystack, needle)


@dataclass(frozen=True)
class DepotContext:
    """Datos del deposito que sesgan y validan la geocodificacion."""

    lat: float | None = None
    lon: float | None = None
    city: str | None = None
    region: str | None = None
    postcode: str | None = None
    country: str | None = None
    #: Direccion libre del depot (reverse-geocode). NO se usa como enrichment.
    address: str | None = None
    #: Radio duro: matches fuera de este km se descartan
    max_distance_km: float = 500.0
    #: IANA TZ del depot (solo para ventanas horarias; no sesga geocode)
    timezone: str | None = None

    @property
    def origin(self) -> tuple[float, float] | None:
        if self.lat is None or self.lon is None:
            return None
        return (float(self.lat), float(self.lon))

    def enrichment_tokens(self) -> list[str]:
        """Tokens a anexar a la query: solo localidad estructurada.

        Orden: city → region → country → postcode (si el cliente lo mando).
        Sin origin hints por bbox, sin parsear `address` libre.
        Si faltan tokens, el caller debe haber corrido `fill_depot_from_index`.
        """
        tokens: list[str] = []
        seen: set[str] = set()

        def _add(raw: str | None, *, allow_postcode: bool = False) -> None:
            if allow_postcode and raw and str(raw).strip():
                t = str(raw).strip()
            else:
                t = normalize_locality_label(raw)
            if not t:
                return
            key = fold(t)
            if key in seen:
                return
            seen.add(key)
            tokens.append(t)

        for value in (self.city, self.region, self.country):
            _add(value)
        if self.postcode:
            _add(self.postcode, allow_postcode=True)
        return tokens

    def enrich_address(self, address: str) -> str:
        """Inyecta ciudad/provincia/pais del depot cuando faltan; sin duplicar CABA.

        Si la dirección ya nombra OTRA ciudad (Fredericia, Coquimbo, Ploiești),
        no se pega la del depot: eso convierte un homónimo en la capital en
        un verde a 100+ km.
        """
        raw = dedupe_address_segments((address or "").strip())
        if not raw:
            return raw
        extras: list[str] = []
        for token in self.enrichment_tokens():
            if already_present(raw, token):
                continue
            if self.city and token == self.city and _address_names_other_city(raw, token):
                continue
            extras.append(token)
        if not extras:
            return raw
        return dedupe_address_segments(f"{raw}, {', '.join(extras)}")

    def distance_km(self, lat: float, lon: float) -> float | None:
        if self.origin is None:
            return None
        return haversine_km(self.origin[0], self.origin[1], lat, lon)

    def within_operating_radius(self, lat: float, lon: float) -> bool:
        """False si el resultado cae fuera del radio maximo del depot."""
        dist = self.distance_km(lat, lon)
        if dist is None:
            return True  # sin origen no hay geofence
        return dist <= float(self.max_distance_km)

    def as_dict(self) -> dict:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "city": self.city,
            "region": self.region,
            "postcode": self.postcode,
            "country": self.country,
            "address": self.address,
            "max_distance_km": self.max_distance_km,
            "timezone": self.timezone,
            "enrichment_tokens": self.enrichment_tokens(),
        }


def align_depot_to_geolocator(depot: DepotContext | None) -> DepotContext | None:
    """El geolocalizador (city) manda el mapa si el pin del depot queda en otro continente.

    Si el form dice Near Miami y el depot sigue en CABA, buscar en el índice de
    Argentina y/o aplicar geofence de 500 km tumba todos los pines (~7000 km).
    El centroide de la city pasa a ser el origin; city/country no se tocan.
    """
    if depot is None or not (depot.city or "").strip():
        return depot
    from .city_lookup import lookup_city_centroid

    centroid = lookup_city_centroid(depot.city, depot.country)
    if centroid is None:
        return depot
    if depot.origin is None:
        return DepotContext(
            lat=centroid[0], lon=centroid[1],
            city=depot.city, region=depot.region, postcode=depot.postcode,
            country=depot.country, address=depot.address,
            max_distance_km=depot.max_distance_km, timezone=depot.timezone,
        )
    far_km = haversine_km(depot.lat, depot.lon, centroid[0], centroid[1])
    if far_km <= float(depot.max_distance_km):
        return depot
    return DepotContext(
        lat=centroid[0], lon=centroid[1],
        city=depot.city, region=depot.region, postcode=depot.postcode,
        country=depot.country, address=depot.address,
        max_distance_km=depot.max_distance_km, timezone=depot.timezone,
    )


def strip_conflicting_depot_city(
    depot: DepotContext | None,
    origin: tuple[float, float] | None,
    *,
    near_city: str | None = None,
    max_distance_km: float = 500.0,
) -> tuple[DepotContext | None, str | None]:
    """Saca city/país del enrich si no coinciden con el punto del corpus.

    `--depot-city San Francisco` sobre un JSON de CABA ensucia cada query
    (`…, san francisco, Argentina`) y tumba pines que el índice sí tiene.
    San Francisco (Córdoba) puede quedar < 500 km de CABA: el nombre del
    índice manda, no el geofence.
    """
    if depot is None or not (depot.city or "").strip():
        return depot, None

    city = depot.city.strip()
    if near_city:
        # NYC vs Jamaica (barrio/USPS Queens): mismo metro, no San Francisco vs CABA.
        if already_present(near_city, city) or already_present(city, near_city):
            return depot, None
        warning = (
            f"--depot-city {city!r} no coincide con el índice ({near_city}); "
            "no la inyecto"
        )
        return _depot_without_locality(depot), warning

    if origin is None:
        return depot, None

    from .city_lookup import lookup_city_centroid
    centroid = lookup_city_centroid(city, depot.country)
    if centroid is None:
        return depot, None
    far_km = haversine_km(origin[0], origin[1], centroid[0], centroid[1])
    if far_km <= float(max_distance_km):
        return depot, None
    warning = (
        f"--depot-city {city!r} queda a {far_km:.0f} km del corpus; "
        "no la inyecto (uso localidad del índice)"
    )
    return _depot_without_locality(depot), warning


def _address_names_other_city(address: str, depot_city: str) -> bool:
    """True si el address ya nombra una ciudad que no es la del depot."""
    from .scoring import _comma_place_labels, _place_labels_overlap
    from ..resources import fold
    from .address import normalize_text

    places = _comma_place_labels(address)
    if not places:
        return False
    depot = {fold(normalize_text(depot_city))}
    return not _place_labels_overlap(places, depot)


def _depot_without_locality(depot: DepotContext) -> DepotContext:
    return DepotContext(
        lat=depot.lat, lon=depot.lon,
        city=None, region=None, postcode=None, country=None,
        address=depot.address,
        max_distance_km=depot.max_distance_km, timezone=depot.timezone,
    )


def depot_from_params(
    *,
    origin_lat: float | None = None,
    origin_lon: float | None = None,
    depot_city: str | None = None,
    depot_region: str | None = None,
    depot_postcode: str | None = None,
    depot_country: str | None = None,
    depot_address: str | None = None,
    depot_timezone: str | None = None,
    max_distance_km: float = 500.0,
) -> DepotContext | None:
    """Construye DepotContext desde query params opcionales (retrocompatible)."""
    has_origin = origin_lat is not None and origin_lon is not None
    has_text = any(
        (v or "").strip()
        for v in (depot_city, depot_region, depot_postcode, depot_country, depot_address)
    )
    if not has_origin and not has_text:
        return None
    return DepotContext(
        lat=float(origin_lat) if origin_lat is not None else None,
        lon=float(origin_lon) if origin_lon is not None else None,
        city=(depot_city or "").strip() or None,
        region=(depot_region or "").strip() or None,
        postcode=(depot_postcode or "").strip() or None,
        country=(depot_country or "").strip() or None,
        address=(depot_address or "").strip() or None,
        timezone=(depot_timezone or "").strip() or None,
        max_distance_km=float(max_distance_km),
    )
