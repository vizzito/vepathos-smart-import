"""Generar corpus de direcciones con ground truth para geocode-truth / traps.

Fuentes:
  * OpenAddresses Batch API (GeoJSON.gz): calle, altura, lat/lng verificados.
  * Fixture local CSV (sin token / sin red).
  * TSV parser_text: solo texto — normalize, no geocode-accuracy.

OpenAddresses migró a https://batch.openaddresses.io/ — las URLs
data.openaddresses.io/runs/…/countrywide.zip ya no existen (404).
Descarga GeoJSON: GET /api/job/{id}/output/source.geojson.gz con Bearer token
(gratis tras crear cuenta: Profile → API token → OPENADDRESSES_TOKEN).
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import random
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

OPENADDRESSES_BATCH = "https://batch.openaddresses.io"
OPENADDRESSES_TOKEN_ENV = "OPENADDRESSES_TOKEN"

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class OAPreset:
    """Preset = slug de fuente en batch.openaddresses.io (no URL legacy)."""

    slug: str
    country: str | None = None
    note: str = ""
    max_gz_mb: float = 80.0
    depot_city: str | None = None
    depot_country: str | None = None
    depot_lat: float | None = None
    depot_lon: float | None = None
    lang: str | None = None


def _oa(slug: str, country: str, note: str, *, city: str, depot_country: str,
        lat: float, lon: float, max_gz_mb: float = 80.0,
        lang: str | None = None) -> OAPreset:
    return OAPreset(
        slug, country, note, max_gz_mb=max_gz_mb,
        depot_city=city, depot_country=depot_country,
        depot_lat=lat, depot_lon=lon, lang=lang)


_COUNTRY_LABELS: dict[str, str] = {
    "AE": "United Arab Emirates",
    "AR": "Argentina",
    "AT": "Austria",
    "AU": "Australia",
    "BE": "Belgium",
    "BR": "Brasil",
    "CA": "Canada",
    "CH": "Switzerland",
    "CL": "Chile",
    "CO": "Colombia",
    "CZ": "Czechia",
    "DE": "Germany",
    "DK": "Denmark",
    "ES": "España",
    "FI": "Finland",
    "FR": "France",
    "IT": "Italy",
    "JP": "Japan",
    "LU": "Luxembourg",
    "MX": "México",
    "NL": "Netherlands",
    "NO": "Norway",
    "NZ": "New Zealand",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "SE": "Sweden",
    "SG": "Singapore",
    "US": "United States",
    "UY": "Uruguay",
    "ZA": "South Africa",
}


# Ciudades / regiones chicas (no countrywide gigante). OA no tiene GB, IE, IN, PE.
OPENADDRESSES_PRESETS: dict[str, OAPreset] = {
    "argentina": _oa(
        "ar/c/city_of_buenos_aires", "AR", "CABA (~560k dirs)",
        city="CABA", depot_country="Argentina",
        lat=-34.598, lon=-58.416, lang="es"),
    "mexico": _oa(
        "mx/jal/statewide", "MX", "Jalisco statewide",
        city="Guadalajara", depot_country="México",
        lat=20.6597, lon=-103.3496, max_gz_mb=120.0, lang="es"),
    "spain": _oa(
        "es/25829", "ES", "Fuente ES regional (evita countrywide)",
        city="Madrid", depot_country="España",
        lat=40.4168, lon=-3.7038, lang="es"),
    "colombia": _oa(
        "co/ant/medellin", "CO", "Medellín",
        city="Medellín", depot_country="Colombia",
        lat=6.2442, lon=-75.5812, lang="es"),
    "chile": _oa(
        "cl/countrywide", "CL", "Chile countrywide (puede ser grande)",
        city="Santiago", depot_country="Chile",
        lat=-33.4489, lon=-70.6693, max_gz_mb=150.0, lang="es"),
    "uruguay": _oa(
        "uy/mo/montevideo", "UY", "Montevideo",
        city="Montevideo", depot_country="Uruguay",
        lat=-34.9011, lon=-56.1645, lang="es"),
    "us_california": _oa(
        "us/ca/san_francisco", "US", "San Francisco city",
        city="San Francisco", depot_country="United States",
        lat=37.7749, lon=-122.4194, lang="en"),
    "canada": _oa(
        "ca/on/city_of_toronto", "CA", "Toronto",
        city="Toronto", depot_country="Canada",
        lat=43.6532, lon=-79.3832, lang="en"),
    "australia": _oa(
        "au/vic/city_of_melbourne", "AU", "Melbourne",
        city="Melbourne", depot_country="Australia",
        lat=-37.8136, lon=144.9631, lang="en"),
    "new_zealand": _oa(
        "nz/city_of_dunedin", "NZ", "Dunedin",
        city="Dunedin", depot_country="New Zealand",
        lat=-45.8788, lon=170.5028, lang="en"),
    "south_africa": _oa(
        "za/wc/cape_town", "ZA", "Cape Town",
        city="Cape Town", depot_country="South Africa",
        lat=-33.9249, lon=18.4241, lang="en"),
    "singapore": _oa(
        "sg/countrywide", "SG", "Singapore",
        city="Singapore", depot_country="Singapore",
        lat=1.3521, lon=103.8198, max_gz_mb=120.0, lang="en"),
    "brazil": _oa(
        "br/sp/sao-paulo-city", "BR", "São Paulo city",
        city="São Paulo", depot_country="Brasil",
        lat=-23.5505, lon=-46.6333, lang="pt"),
    "portugal": _oa(
        "pt/countrywide", "PT", "Portugal countrywide",
        city="Lisboa", depot_country="Portugal",
        lat=38.7223, lon=-9.1393, max_gz_mb=150.0, lang="pt"),
    "france": _oa(
        "fr/75/statewide", "FR", "Departamento 75 (París)",
        city="Paris", depot_country="France",
        lat=48.8566, lon=2.3522, lang="fr"),
    "belgium": _oa(
        "be/bru/bosa-region-brussels-fr", "BE", "Bruselas (FR)",
        city="Bruxelles", depot_country="Belgium",
        lat=50.8503, lon=4.3517, lang="fr"),
    "switzerland": _oa(
        "ch/geneva", "CH", "Ginebra",
        city="Geneva", depot_country="Switzerland",
        lat=46.2044, lon=6.1432, lang="fr"),
    "italy": _oa(
        "it/45/bologna", "IT", "Bologna",
        city="Bologna", depot_country="Italy",
        lat=44.4949, lon=11.3426, lang="it"),
    "germany": _oa(
        "de/berlin", "DE", "Berlin",
        city="Berlin", depot_country="Germany",
        lat=52.5200, lon=13.4050, lang="de"),
    "austria": _oa(
        "at/city_of_vienna", "AT", "Viena",
        city="Vienna", depot_country="Austria",
        lat=48.2082, lon=16.3738, lang="de"),
    "netherlands": _oa(
        "nl/countrywide", "NL", "Países Bajos countrywide (puede ser grande)",
        city="Amsterdam", depot_country="Netherlands",
        lat=52.3676, lon=4.9041, max_gz_mb=200.0, lang="nl"),
    "poland": _oa(
        "pl/mazowieckie", "PL", "Mazovia (Varsovia)",
        city="Warsaw", depot_country="Poland",
        lat=52.2297, lon=21.0122, max_gz_mb=150.0, lang="pl"),
    "denmark": _oa(
        "dk/countrywide", "DK", "Dinamarca countrywide",
        city="Copenhagen", depot_country="Denmark",
        lat=55.6761, lon=12.5683, max_gz_mb=150.0, lang="en"),
    "sweden": _oa(
        "se/municipality_of_stockholm", "SE", "Estocolmo",
        city="Stockholm", depot_country="Sweden",
        lat=59.3293, lon=18.0686, lang="en"),
    "norway": _oa(
        "no/03/statewide", "NO", "Oslo (fylke 03)",
        city="Oslo", depot_country="Norway",
        lat=59.9139, lon=10.7522, lang="en"),
    "finland": _oa(
        "fi/uusimaa-fi", "FI", "Uusimaa (Helsinki)",
        city="Helsinki", depot_country="Finland",
        lat=60.1699, lon=24.9384, max_gz_mb=120.0, lang="en"),
    "czechia": _oa(
        "cz/countrywide", "CZ", "Chequia countrywide",
        city="Prague", depot_country="Czechia",
        lat=50.0755, lon=14.4378, max_gz_mb=150.0, lang="en"),
    "luxembourg": _oa(
        "lu/countrywide", "LU", "Luxemburgo",
        city="Luxembourg", depot_country="Luxembourg",
        lat=49.6116, lon=6.1319, lang="fr"),
    "romania": _oa(
        "ro/bucharest", "RO", "Bucarest",
        city="Bucharest", depot_country="Romania",
        lat=44.4268, lon=26.1025, lang="en"),
    "japan": _oa(
        "jp/tokyo", "JP", "Tokio",
        city="Tokyo", depot_country="Japan",
        lat=35.6762, lon=139.6503, max_gz_mb=150.0, lang="en"),
    "uae": _oa(
        "ae/du/dubai-en", "AE", "Dubai (EN)",
        city="Dubai", depot_country="United Arab Emirates",
        lat=25.2048, lon=55.2708, lang="en"),
}

# Mercados de los idiomas del parser (es/en/pt/fr/it/de/nl/pl) + extras OA.
# OA no publica GB, IE, IN, PE, etc.
LANGUAGE_PACK: tuple[str, ...] = (
    "argentina", "mexico", "spain", "colombia", "chile", "uruguay",
    "us_california", "canada", "australia", "new_zealand", "south_africa",
    "singapore",
    "brazil", "portugal",
    "france", "belgium", "switzerland", "luxembourg",
    "italy",
    "germany", "austria",
    "netherlands",
    "poland",
    "denmark", "sweden", "norway", "finland", "czechia", "romania",
    "japan", "uae",
)

PRESET_PACKS: dict[str, tuple[str, ...]] = {
    "languages": LANGUAGE_PACK,
    "all": tuple(sorted(OPENADDRESSES_PRESETS)),
}


def preset_for_group(key: str) -> OAPreset | None:
    """Preset del mix: nombre (`france`) o ISO (`FR`)."""
    raw = (key or "").strip()
    if not raw:
        return None
    if raw in OPENADDRESSES_PRESETS:
        return OPENADDRESSES_PRESETS[raw]
    fold = raw.upper()
    for preset in OPENADDRESSES_PRESETS.values():
        if (preset.country or "").upper() == fold:
            return preset
    return None

LOCAL_OA_FIXTURES: dict[str, Path] = {
    "argentina": ROOT / "examples/geocode-truth/sources/oa_caba_sample.csv",
}

LIBPOSTAL_DEFAULT_TSV = ROOT / "examples/geocode-truth/sources/parser_multilang_sample.tsv"

_OA_WANTED = frozenset({
    "number", "street", "unit", "city", "district", "region",
    "postcode", "lon", "lat", "hash", "id",
})

ADDRESS_STYLES = ("oa_default", "us", "eu", "minimal", "full")


class OpenAddressesError(RuntimeError):
    """Error de lookup o descarga OpenAddresses."""


@dataclass
class CorpusRecord:
    address: str
    lat: float | None = None
    lng: float | None = None
    street: str | None = None
    number: str | None = None
    unit: str | None = None
    city: str | None = None
    region: str | None = None
    postcode: str | None = None
    country: str | None = None
    source: str = ""
    source_id: str | None = None
    style: str = "oa_default"
    has_coords: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "address": self.address,
            "lat": self.lat,
            "lng": self.lng,
            "street": self.street,
            "number": self.number,
            "city": self.city,
            "region": self.region,
            "postcode": self.postcode,
            "country": self.country,
            "style": self.style,
            "source": self.source,
        }
        if self.source_id:
            out["source_id"] = self.source_id
        if self.unit:
            out["unit"] = self.unit
        if self.extra:
            out.update(self.extra)
        if not self.has_coords:
            out["has_coords"] = False
        return out


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_float(value: Any) -> float | None:
    text = _clean(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _normalize_oa_row(raw: dict[str, Any]) -> dict[str, str] | None:
    mapped: dict[str, str] = {}
    for key, val in raw.items():
        if key is None:
            continue
        fold = str(key).strip().lower()
        if fold in _OA_WANTED:
            cleaned = _clean(val)
            if cleaned:
                mapped[fold] = cleaned

    street = mapped.get("street")
    number = mapped.get("number")
    lat = _parse_float(mapped.get("lat"))
    lng = _parse_float(mapped.get("lon"))
    if not street or not number or lat is None or lng is None:
        return None
    if street.strip().lower() == "street" and number.strip().lower() == "number":
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    if abs(lat) < 1e-5 and abs(lng) < 1e-5:
        return None
    mapped["_lat_f"] = str(lat)
    mapped["_lng_f"] = str(lng)
    return mapped


def _normalize_oa_feature(feature: dict[str, Any]) -> dict[str, str] | None:
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates")
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        row = dict(props)
        row["lon"] = coords[0]
        row["lat"] = coords[1]
        return _normalize_oa_row(row)
    return _normalize_oa_row(props)


def build_address(row: dict[str, str], style: str = "oa_default") -> str:
    street = row.get("street") or ""
    number = row.get("number") or ""
    unit = row.get("unit")
    city = row.get("city")
    region = row.get("region")
    postcode = row.get("postcode")

    parts: list[str] = []
    if style == "us":
        core = f"{number} {street}".strip()
        if unit:
            core = f"{core} Apt {unit}"
        parts.append(core)
        if city:
            parts.append(city)
        tail = " ".join(p for p in (region, postcode) if p)
        if tail:
            parts.append(tail)
    elif style == "eu":
        core = f"{street} {number}".strip()
        if unit:
            core = f"{core}, {unit}"
        parts.append(core)
        if postcode and city:
            parts.append(f"{postcode} {city}")
        elif city:
            parts.append(city)
    elif style == "minimal":
        parts.append(f"{street} {number}".strip())
        if city:
            parts.append(city)
    elif style == "full":
        parts.append(f"{street} {number}".strip())
        if unit:
            parts.append(f"Depto {unit}")
        for token in (city, region, postcode):
            if token:
                parts.append(token)
    else:
        parts.append(street)
        parts.append(number)
        if unit:
            parts.append(f"Apt {unit}")
        if city:
            parts.append(city)
        if postcode:
            parts.append(postcode)

    return ", ".join(p for p in parts if p)


def oa_row_to_record(row: dict[str, str], *, source: str, style: str,
                     country: str | None = None) -> CorpusRecord:
    from ..geocoding.coord_check import apply_country_coord_fix

    lat = float(row["_lat_f"])
    lng = float(row["_lng_f"])
    lat, lng, coord_fix = apply_country_coord_fix(lat, lng, country)
    source_id = row.get("hash") or row.get("id")
    extra = {"coord_fix": coord_fix} if coord_fix else {}
    return CorpusRecord(
        address=build_address(row, style),
        lat=lat,
        lng=lng,
        street=row.get("street"),
        number=row.get("number"),
        unit=row.get("unit"),
        city=row.get("city"),
        region=row.get("region"),
        postcode=row.get("postcode"),
        country=country,
        source=source,
        source_id=source_id,
        style=style,
        has_coords=True,
        extra=extra,
    )


def iter_openaddresses_csv(path: Path) -> Iterator[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if not row:
                continue
            norm = _normalize_oa_row(row)
            if norm:
                yield norm


def iter_openaddresses_rows(zf: zipfile.ZipFile) -> Iterator[dict[str, str]]:
    csv_files = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if not csv_files:
        raise ValueError("no hay CSV dentro del ZIP de OpenAddresses")
    target = max(csv_files, key=lambda n: zf.getinfo(n).file_size)
    with zf.open(target) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        reader = csv.DictReader(text)
        for row in reader:
            if not row:
                continue
            norm = _normalize_oa_row(row)
            if norm:
                yield norm


def iter_geojson_features(data: bytes, *, compressed: bool = True) -> Iterator[dict[str, str]]:
    """FeatureCollection JSON o NDJSON (una Feature por línea, formato batch OA)."""

    def _yield_feature(feature: dict[str, Any]) -> Iterator[dict[str, str]]:
        if not isinstance(feature, dict):
            return
        norm = _normalize_oa_feature(feature)
        if norm:
            yield norm

    def _text_lines() -> Iterator[str]:
        if compressed:
            with gzip.open(io.BytesIO(data), "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    yield line
        else:
            for line in data.decode("utf-8", errors="replace").splitlines():
                yield line + "\n"

    lines = _text_lines()
    try:
        first = next(lines).strip()
    except StopIteration:
        return

    if first.startswith('{"type":"Feature"') or first.startswith('{"type": "Feature"'):
        try:
            yield from _yield_feature(json.loads(first))
        except json.JSONDecodeError:
            pass
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                yield from _yield_feature(json.loads(line))
            except json.JSONDecodeError:
                continue
        return

    text = first + "".join(lines)
    payload = json.loads(text)
    features = payload.get("features") or []
    if not isinstance(features, list):
        raise ValueError("GeoJSON sin lista features")
    for feature in features:
        yield from _yield_feature(feature)


def reservoir_sample(iterator: Iterator[Any], sample_size: int,
                     rng: random.Random) -> list[Any]:
    if sample_size <= 0:
        return []
    reservoir: list[Any] = []
    for i, item in enumerate(iterator):
        if i < sample_size:
            reservoir.append(item)
            continue
        j = rng.randint(0, i)
        if j < sample_size:
            reservoir[j] = item
    return reservoir


def _records_from_rows(rows: list[dict[str, str]], *, source_label: str,
                       country: str | None, styles: tuple[str, ...]) -> list[CorpusRecord]:
    records: list[CorpusRecord] = []
    for row in rows:
        for style in styles:
            records.append(oa_row_to_record(
                row, source=source_label, style=style, country=country))
    return records


def load_openaddresses_fixture(preset: str, *, sample_size: int, seed: int,
                               country: str | None = None,
                               styles: tuple[str, ...] = ("oa_default",),
                               ) -> list[CorpusRecord]:
    path = LOCAL_OA_FIXTURES.get(preset)
    if path is None or not path.is_file():
        raise OpenAddressesError(
            f"no hay fixture local para preset '{preset}'. "
            f"Disponibles: {', '.join(sorted(LOCAL_OA_FIXTURES))}")
    rng = random.Random(seed)
    rows = reservoir_sample(iter_openaddresses_csv(path), sample_size, rng)
    label = f"fixture:{preset}"
    return _records_from_rows(
        rows, source_label=label, country=country, styles=styles)


def load_openaddresses_zip(data: bytes, *, sample_size: int, seed: int,
                           source_label: str,
                           country: str | None = None,
                           styles: tuple[str, ...] = ("oa_default",),
                           ) -> list[CorpusRecord]:
    rng = random.Random(seed)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        sampled = reservoir_sample(iter_openaddresses_rows(zf), sample_size, rng)
    return _records_from_rows(sampled, source_label=source_label,
                              country=country, styles=styles)


def _auth_headers(token: str | None) -> dict[str, str]:
    headers = {"User-Agent": "vepathos-smart-import/0.1", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_bytes(url: str, *, timeout: float = 120.0,
                token: str | None = None) -> bytes:
    if token and "batch.openaddresses.io" in url and "/output/" in url:
        return _fetch_oa_asset(url, timeout=timeout, token=token)
    req = urllib.request.Request(url, headers=_auth_headers(token))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_oa_asset(url: str, *, timeout: float, token: str) -> bytes:
    """GeoJSON.gz: Bearer solo en batch.openaddresses.io; CDN/R2 sin auth."""
    current = url
    bearer = token
    for _ in range(10):
        headers = {"User-Agent": "vepathos-smart-import/0.1", "Accept": "*/*"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(
                    urllib.request.Request(current, headers=headers),
                    timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                nxt = exc.headers.get("Location")
                if not nxt:
                    raise OpenAddressesError(
                        f"redirect sin Location descargando {current}") from exc
                current = urllib.parse.urljoin(current, nxt)
                bearer = None
                continue
            raise
    raise OpenAddressesError(f"demasiados redirects descargando {url}")


def fetch_json(url: str, *, timeout: float = 60.0,
               token: str | None = None) -> Any:
    raw = fetch_bytes(url, timeout=timeout, token=token)
    return json.loads(raw.decode("utf-8"))


def lookup_oa_job(source_slug: str, *, token: str | None = None) -> dict[str, Any]:
    params = urllib.parse.urlencode({
        "source": source_slug,
        "layer": "addresses",
        "validated": "false",
    })
    url = f"{OPENADDRESSES_BATCH}/api/data?{params}"
    payload = fetch_json(url, token=token)
    if not isinstance(payload, list) or not payload:
        raise OpenAddressesError(f"fuente desconocida o sin datos: {source_slug}")
    entry = payload[0]
    job = entry.get("job") or entry.get("latest_job")
    if not job:
        raise OpenAddressesError(f"sin job activo para {source_slug}")
    return {"job": int(job), "source": entry.get("source") or source_slug,
            "size": entry.get("size")}


def oa_geojson_url(job_id: int) -> str:
    return f"{OPENADDRESSES_BATCH}/api/job/{job_id}/output/source.geojson.gz"


def load_cached_or_download(url: str, cache_path: Path | None, *,
                            timeout: float = 120.0,
                            token: str | None = None) -> bytes:
    if cache_path and cache_path.is_file():
        return cache_path.read_bytes()
    data = fetch_bytes(url, timeout=timeout, token=token)
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(data)
    return data


def load_openaddresses_batch(source_slug: str, *, sample_size: int, seed: int,
                             token: str | None,
                             country: str | None = None,
                             styles: tuple[str, ...] = ("oa_default",),
                             cache_path: Path | None = None,
                             max_gz_mb: float = 80.0,
                             allow_large: bool = False,
                             ) -> tuple[list[CorpusRecord], str, int]:
    """Descarga GeoJSON.gz desde batch API y muestrea."""
    token = token or os.environ.get(OPENADDRESSES_TOKEN_ENV)
    if not token:
        raise OpenAddressesError(
            "OpenAddresses Batch requiere token Bearer.\n"
            "  1. https://batch.openaddresses.io/login (cuenta gratis)\n"
            "  2. Profile → Create API token\n"
            f"  3. export {OPENADDRESSES_TOKEN_ENV}=oa.xxxx\n"
            "  O usa --fixture para muestra local sin red.")

    meta = lookup_oa_job(source_slug, token=token)
    job_id = meta["job"]
    url = oa_geojson_url(job_id)
    size = meta.get("size")
    if size and not allow_large:
        gz_mb = int(size) / (1024 * 1024)
        if gz_mb > max_gz_mb:
            raise OpenAddressesError(
                f"fuente {source_slug} pesa ~{gz_mb:.0f} MB gzip "
                f"(limite {max_gz_mb:.0f} MB). Usa --allow-large o un preset regional.")

    data = load_cached_or_download(url, cache_path, token=token, timeout=300.0)
    gz_mb = len(data) / (1024 * 1024)
    if not allow_large and gz_mb > max_gz_mb:
        raise OpenAddressesError(
            f"descarga {gz_mb:.0f} MB gzip > limite {max_gz_mb:.0f} MB. "
            "Usa --allow-large si sabes lo que haces.")

    rng = random.Random(seed)
    sampled = reservoir_sample(iter_geojson_features(data), sample_size, rng)
    label = f"openaddresses:{source_slug}"
    records = _records_from_rows(sampled, source_label=label,
                                country=country, styles=styles)
    return records, url, job_id


def token_help_message() -> str:
    return (
        f"export {OPENADDRESSES_TOKEN_ENV}=oa.xxxx   # batch.openaddresses.io → Profile\n"
        "python scripts/generate_address_corpus.py --preset argentina -n 50\n"
        "# sin token:\n"
        "python scripts/generate_address_corpus.py --preset argentina --fixture"
    )


def _parse_parser_tsv_line(line: str) -> tuple[str, str | None, str | None]:
    parts = [p.strip() for p in line.split("\t") if p.strip()]
    if not parts:
        return "", None, None
    if len(parts) >= 3 and len(parts[0]) <= 5 and len(parts[1]) <= 3:
        return parts[2], parts[0], parts[1]
    if len(parts) >= 2:
        return parts[1], parts[0], None
    return parts[0], None, None


def load_parser_text_corpus(source: str | Path, *, sample_size: int | None = None,
                            seed: int = 42) -> list[CorpusRecord]:
    path = Path(source)
    if path.is_file():
        raw = path.read_text(encoding="utf-8", errors="replace")
    else:
        raw = fetch_bytes(str(source), timeout=60.0).decode("utf-8", errors="replace")

    lines = [ln for ln in raw.splitlines() if ln.strip() and not ln.startswith("#")]
    rng = random.Random(seed)
    if sample_size is not None and sample_size < len(lines):
        lines = rng.sample(lines, sample_size)

    out: list[CorpusRecord] = []
    for i, line in enumerate(lines, start=1):
        addr, locale, country = _parse_parser_tsv_line(line)
        if not addr:
            continue
        extra: dict[str, Any] = {}
        if locale:
            extra["locale_hint"] = locale
        if country:
            extra["country_hint"] = country
        out.append(CorpusRecord(
            address=addr,
            source="parser_text",
            source_id=f"pt_{i}",
            style="raw",
            has_coords=False,
            extra=extra,
        ))
    return out


def tag_record_preset(record: CorpusRecord, preset: str) -> CorpusRecord:
    """Marca el preset de origen (mix de varios países)."""
    extra = dict(record.extra)
    extra["preset"] = preset
    record.extra = extra
    return record


def resolve_sample_seed(seed: int | None, *, new_sample: bool = False) -> int:
    """Seed del reservoir. `--new-sample` elige uno fresco; si no, 42 (reproducible)."""
    if new_sample:
        return random.SystemRandom().randint(1, 2_147_483_647)
    return 42 if seed is None else int(seed)


_SEED_STEM = re.compile(r"_s\d+$")
_NOISE_IN_STEM = re.compile(r"_noise\d+")


def sample_filename_tag(sample_size: int, seed: int,
                        noise_level: int | None = None) -> str:
    """Siempre incluye seed: `n100_s42` / `n100_noise5_s42`."""
    noise = f"_noise{int(noise_level)}" if noise_level else ""
    return f"n{sample_size}{noise}_s{seed}"


def with_corpus_suffixes(
    path: Path,
    seed: int | None = None,
    noise_level: int | None = None,
) -> Path:
    """`foo_n100.json` → `foo_n100_noise5_s42.json` (no pisa seed ni noise)."""
    stem = path.stem
    seed_m = _SEED_STEM.search(stem)
    head = stem[:seed_m.start()] if seed_m else stem
    tail_seed = seed_m.group(0) if seed_m else ""
    if noise_level and not _NOISE_IN_STEM.search(head):
        head = f"{head}_noise{int(noise_level)}"
    if not tail_seed and seed is not None:
        tail_seed = f"_s{int(seed)}"
    return path.with_name(f"{head}{tail_seed}{path.suffix}")


def with_seed_suffix(path: Path, seed: int) -> Path:
    """`foo_n100.json` → `foo_n100_s42.json` (no duplica si ya está)."""
    return with_corpus_suffixes(path, seed=seed)


def mix_corpus_records(groups: list[list[CorpusRecord]], *,
                       seed: int = 42) -> list[CorpusRecord]:
    """Junta grupos (un país c/u) y baraja con seed estable."""
    merged: list[CorpusRecord] = []
    for group in groups:
        merged.extend(group)
    rng = random.Random(seed)
    rng.shuffle(merged)
    return merged


def write_json_corpus(records: list[CorpusRecord], path: Path, *,
                      note: str, preset: str | None = None,
                      url: str | None = None,
                      pack: str | None = None,
                      countries: list[str] | None = None,
                      seed: int | None = None) -> None:
    with_coords = sum(1 for r in records if r.has_coords)
    payload = {
        "source": "generate_address_corpus",
        "preset": preset,
        "url": url,
        "note": note,
        "rows": len(records),
        "with_coords": with_coords,
        "stops": [r.as_dict() for r in records],
    }
    if seed is not None:
        payload["seed"] = seed
    if pack:
        payload["pack"] = pack
    if countries:
        payload["countries"] = countries
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def write_csv_corpus(records: list[CorpusRecord], path: Path) -> None:
    fields = [
        "address", "address_clean", "noise_level", "noise_variant",
        "lat", "lng", "street", "number", "unit",
        "city", "region", "postcode", "country", "preset", "style",
        "source", "source_id",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            row = rec.as_dict()
            for key in ("address_clean", "noise_level", "noise_variant"):
                if key not in row and key in rec.extra:
                    row[key] = rec.extra[key]
            writer.writerow({k: row.get(k, "") for k in fields})


def slugify(name: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "_", name.strip().lower())
    return text.strip("_") or "corpus"


def _cli_opt(flag: str, value: str) -> str:
    if any(c.isspace() for c in value):
        return f'{flag} "{value}"'
    return f"{flag} {value}"


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def suggest_accuracy_flags(
    records: list[CorpusRecord],
    *,
    preset: str | None = None,
) -> str:
    """Flags de `geocode-accuracy` acorde al corpus (no hardcodear CABA)."""
    meta = OPENADDRESSES_PRESETS.get(preset or "")
    city = meta.depot_city if meta else None
    country = meta.depot_country if meta else None
    lat = meta.depot_lat if meta else None
    lon = meta.depot_lon if meta else None

    if not city:
        cities = [r.city for r in records if r.city]
        if cities:
            city = Counter(cities).most_common(1)[0][0]
    if not country:
        for rec in records:
            if rec.country:
                country = _COUNTRY_LABELS.get(rec.country.upper(), rec.country)
                break
    if lat is None or lon is None:
        lats = [r.lat for r in records if r.lat is not None]
        lons = [r.lng for r in records if r.lng is not None]
        if lats and lons:
            lat = _median(lats)
            lon = _median(lons)
    if lat is None or lon is None:
        return ""

    parts: list[str] = []
    if city:
        parts.append(_cli_opt("--depot-city", city))
    if country:
        parts.append(_cli_opt("--depot-country", country))
    parts.append(f"--origin-lat {lat:.4f}")
    parts.append(f"--origin-lon {lon:.4f}")
    return " ".join(parts)


@dataclass(frozen=True)
class TruthGenerateSpec:
    """Parámetros inferidos de un nombre tipo argentina_fixture_n150.json."""

    preset: str
    sample_size: int
    use_fixture: bool
    styles: tuple[str, ...] = ("oa_default",)


def parse_truth_generate_spec(path: Path) -> TruthGenerateSpec | None:
    """Infiera preset / n / fixture desde el nombre del archivo truth."""
    name = path.name
    if not name.lower().endswith(".json"):
        return None
    stem = name[:-5]
    for preset in sorted(OPENADDRESSES_PRESETS, key=len, reverse=True):
        for use_fixture, prefix in ((True, f"{preset}_fixture_n"), (False, f"{preset}_n")):
            if not stem.startswith(prefix):
                continue
            tail = stem[len(prefix):]
            if tail.isdigit():
                return TruthGenerateSpec(
                    preset=preset,
                    sample_size=int(tail),
                    use_fixture=use_fixture,
                )
    return None


def generate_truth_corpus(
    path: Path,
    spec: TruthGenerateSpec,
    *,
    seed: int = 42,
    token: str | None = None,
    cache_dir: Path | None = None,
    allow_large: bool = False,
) -> tuple[list[CorpusRecord], str, str]:
    """Genera registros para un truth JSON (fixture local o OpenAddresses Batch)."""
    preset_meta = OPENADDRESSES_PRESETS[spec.preset]
    country = preset_meta.country
    cache_dir = cache_dir or ROOT / "data" / "openaddresses"
    token = token or os.environ.get(OPENADDRESSES_TOKEN_ENV)

    if spec.use_fixture:
        records = load_openaddresses_fixture(
            spec.preset, sample_size=spec.sample_size, seed=seed,
            country=country, styles=spec.styles)
        fixture_path = LOCAL_OA_FIXTURES.get(spec.preset)
        url = str(fixture_path) if fixture_path else ""
        note = (
            f"Fixture local (seed={seed}). Sin OpenAddresses Batch. "
            f"Muestra pedida n={spec.sample_size}, obtenidas {len(records)}."
        )
        return records, url, note

    slug = preset_meta.slug
    cache_path = cache_dir / f"{slugify(slug)}.geojson.gz"
    try:
        records, url, job_id = load_openaddresses_batch(
            slug, sample_size=spec.sample_size, seed=seed, token=token,
            country=country, styles=spec.styles, cache_path=cache_path,
            max_gz_mb=preset_meta.max_gz_mb, allow_large=allow_large)
        note = (
            f"Muestra aleatoria (seed={seed}) desde batch.openaddresses.io job {job_id}. "
            f"Estilos: {', '.join(spec.styles)}."
        )
        return records, url, note
    except OpenAddressesError as exc:
        if spec.preset not in LOCAL_OA_FIXTURES:
            raise
        records = load_openaddresses_fixture(
            spec.preset, sample_size=spec.sample_size, seed=seed,
            country=country, styles=spec.styles)
        fixture_path = LOCAL_OA_FIXTURES[spec.preset]
        note = (
            f"Fixture local (fallback sin token: {exc}). seed={seed}. "
            f"Muestra pedida n={spec.sample_size}, obtenidas {len(records)}."
        )
        return records, str(fixture_path), note


def ensure_truth_corpus(
    path: Path,
    *,
    seed: int = 42,
    token: str | None = None,
    cache_dir: Path | None = None,
    allow_large: bool = False,
) -> Path:
    """Crea el JSON truth en `path` si no existe (nombre convencional requerido)."""
    path = path.expanduser().resolve()
    if path.is_file():
        return path

    spec = parse_truth_generate_spec(path)
    if spec is None:
        raise OpenAddressesError(
            f"no existe {path.name} y el nombre no sigue el patron "
            f"{{preset}}[_fixture]_n{{N}}.json "
            f"(presets: {', '.join(sorted(OPENADDRESSES_PRESETS))}).\n"
            f"Generar manualmente:\n"
            f"  python scripts/generate_address_corpus.py --preset argentina --fixture "
            f"-n 50 --out {path}")

    records, url, note = generate_truth_corpus(
        path, spec, seed=seed, token=token, cache_dir=cache_dir,
        allow_large=allow_large)
    if not records:
        raise OpenAddressesError(f"no se obtuvieron filas para {path.name}")

    write_json_corpus(records, path, note=note, preset=spec.preset, url=url)
    if records[0].has_coords:
        write_csv_corpus(records, path.with_suffix(".csv"))
    return path
