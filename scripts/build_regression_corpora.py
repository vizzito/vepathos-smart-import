#!/usr/bin/env python3
"""Corpus congelados para `geocode-regression`, uno por pais/ciudad.

Dos fuentes de verdad, y el corpus dice cual uso:

  openaddresses  lat/lng de OpenAddresses (independiente de OSM). Solo regiones
                 con cache en data/openaddresses/*.geojson.gz: no baja nada.
  osm-addr       nodos addr:street + addr:housenumber del propio indice OSM de la
                 ciudad. La fila LIMPIA es optimista (el punto existe en el
                 indice); lo que mide es el RUIDO: si la misma direccion mal
                 escrita sigue cayendo en su puerta.

Cada direccion base sale limpia + 1 variante por nivel 1-5 + hasta 4 variantes del
nivel 6 (planilla de despacho). Seed fijo: correr dos veces da el mismo archivo.

    .venv/bin/python scripts/build_regression_corpora.py --source oa
    .venv/bin/python scripts/build_regression_corpora.py --source osm --only PE,BR
    .venv/bin/python scripts/build_regression_corpora.py --manifest   # agrega suites
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smart_import.tools.address_corpus import (  # noqa: E402
    OPENADDRESSES_PRESETS, CorpusRecord, _records_from_rows, iter_geojson_features,
)
from smart_import.tools.address_noise import (  # noqa: E402
    _LEVEL_BUILDERS, LEVEL6_KINDS,
)

OUT = ROOT / "examples" / "geocode-truth" / "regression" / "corpora"
MANIFEST = ROOT / "examples" / "geocode-truth" / "regression" / "manifest.json"
OA_CACHE = ROOT / "data" / "openaddresses"

SEED = 42
BASE_PER_REGION = 40
SAMPLE_RADIUS_KM = 6.0
LEVEL6_PER_BASE = 4
#: extract chico: el corpus vive a <= 6 km del depot y el disco no sobra
INDEX_MARGIN_KM = 6.0

#: OpenAddresses: preset → (iso, ciudad). Depot = preset; si el dataset es
#: regional y el depot cae lejos (es/25829 es Galicia), se centra en los datos.
OA_REGIONS = {
    "argentina": "AR", "mexico": "MX", "spain": "ES", "colombia": "CO",
    "chile": "CL", "uruguay": "UY", "us_california": "US", "canada": "CA",
    "australia": "AU", "new_zealand": "NZ", "south_africa": "ZA",
    "singapore": "SG", "france": "FR", "belgium": "BE", "switzerland": "CH",
    "italy": "IT", "germany": "DE", "austria": "AT", "poland": "PL",
    "denmark": "DK", "sweden": "SE", "norway": "NO", "luxembourg": "LU",
    "romania": "RO", "japan": "JP", "uae": "AE",
}
#: fuentes OA cacheadas que no son preset
OA_EXTRA = {
    "us_new_york": ("us/ny/city_of_new_york", "US", "New York", "United States",
                    40.7128, -74.0060),
}

#: OSM: iso → (ciudad, pais para el zone_hint y el depot, lat, lon)
OSM_CITIES = {
    "BR": ("São Paulo", "Brasil", -23.5505, -46.6333),
    "PE": ("Lima", "Peru", -12.0464, -77.0428),
    "EC": ("Quito", "Ecuador", -0.1807, -78.4678),
    "VE": ("Caracas", "Venezuela", 10.4806, -66.9036),
    "BO": ("La Paz", "Bolivia", -16.4897, -68.1193),
    "PY": ("Asunción", "Paraguay", -25.2637, -57.5759),
    "CR": ("San José", "Costa Rica", 9.9281, -84.0907),
    "GT": ("Guatemala", "Guatemala", 14.6349, -90.5069),
    "PA": ("Panamá", "Panama", 8.9824, -79.5199),
    "DO": ("Santo Domingo", "Dominican Republic", 18.4861, -69.9312),
    "ES": ("Madrid", "España", 40.4168, -3.7038),
    "PT": ("Lisboa", "Portugal", 38.7223, -9.1393),
    "NL": ("Amsterdam", "Netherlands", 52.3676, 4.9041),
    "CZ": ("Praha", "Czechia", 50.0755, 14.4378),
    "SK": ("Bratislava", "Slovakia", 48.1486, 17.1077),
    "HU": ("Budapest", "Hungary", 47.4979, 19.0402),
    "BG": ("Sofia", "Bulgaria", 42.6977, 23.3219),
    "GR": ("Athens", "Greece", 37.9838, 23.7275),
    "HR": ("Zagreb", "Croatia", 45.8150, 15.9819),
    "RS": ("Belgrade", "Serbia", 44.7866, 20.4489),
    "SI": ("Ljubljana", "Slovenia", 46.0569, 14.5058),
    "FI": ("Helsinki", "Finland", 60.1699, 24.9384),
    "IE": ("Dublin", "Ireland", 53.3498, -6.2603),
    "GB": ("London", "United Kingdom", 51.5074, -0.1278),
    "UA": ("Kyiv", "Ukraine", 50.4501, 30.5234),
    "TR": ("Istanbul", "Turkey", 41.0082, 28.9784),
    "RU": ("Moscow", "Russia", 55.7558, 37.6173),
    "EE": ("Tallinn", "Estonia", 59.4370, 24.7536),
    "LT": ("Vilnius", "Lithuania", 54.6872, 25.2797),
    "LV": ("Riga", "Latvia", 56.9496, 24.1052),
    "IL": ("Tel Aviv", "Israel", 32.0853, 34.7818),
    "SA": ("Riyadh", "Saudi Arabia", 24.7136, 46.6753),
    "EG": ("Cairo", "Egypt", 30.0444, 31.2357),
    "MA": ("Casablanca", "Morocco", 33.5731, -7.5898),
    "NG": ("Lagos", "Nigeria", 6.5244, 3.3792),
    "KE": ("Nairobi", "Kenya", -1.2921, 36.8219),
    "KR": ("Seoul", "South Korea", 37.5665, 126.9780),
    "TW": ("Taipei", "Taiwan", 25.0330, 121.5654),
    "HK": ("Hong Kong", "Hong Kong", 22.3193, 114.1694),
    "MY": ("Kuala Lumpur", "Malaysia", 3.1390, 101.6869),
    "TH": ("Bangkok", "Thailand", 13.7563, 100.5018),
    "VN": ("Hanoi", "Vietnam", 21.0278, 105.8342),
    "ID": ("Jakarta", "Indonesia", -6.2088, 106.8456),
    "PH": ("Manila", "Philippines", 14.5995, 120.9842),
    "IN": ("Mumbai", "India", 19.0760, 72.8777),
    "CN": ("Shanghai", "China", 31.2304, 121.4737),
}

_EN_ORDER = {"US", "CA", "AU", "NZ", "ZA", "SG", "AE", "GB", "IE", "IN", "NG", "KE",
             "PH", "MY", "HK", "IL", "SA", "EG"}


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


def expand(records: list[CorpusRecord], *, seed: int) -> list[dict]:
    """limpia + 1 variante por nivel 1-5 + hasta 4 del nivel 6."""
    rng = random.Random(seed)
    rows: list[dict] = []
    for base_i, rec in enumerate(records):
        def add(text: str, level: int, kind: str) -> None:
            rows.append({
                "address": text, "lat": rec.lat, "lng": rec.lng,
                "country": rec.country, "noise_level": level, "noise_kind": kind,
                "address_clean": rec.address, "base": base_i,
            })
        add(rec.address, 0, "clean")
        for level in range(1, 6):
            builder = _LEVEL_BUILDERS[level]
            variants = builder(rec, rng, max_permutations=2) if level >= 3 else builder(rec, rng)
            variants = [v for v in variants if v and v != rec.address]
            if variants:
                add(rng.choice(variants), level, f"L{level}")
        l6 = _LEVEL_BUILDERS[6](rec, rng, max_permutations=2)
        rng.shuffle(l6)
        for text, kind in l6[:LEVEL6_PER_BASE]:
            add(text, 6, kind)
    return rows


def write_corpus(path: Path, *, source: str, iso: str, city: str, country: str,
                 depot: tuple[float, float], rows: list[dict], note: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "source": source, "country": iso, "city": city, "country_label": country,
        "depot": {"lat": depot[0], "lon": depot[1], "city": city, "country": country},
        "seed": SEED, "bases": len({r["base"] for r in rows}), "rows_count": len(rows),
        "note": note, "rows": rows,
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def _street_number_clean(rec: CorpusRecord) -> CorpusRecord:
    """Direccion limpia al estilo local: calle+altura, CP ciudad."""
    street, number = (rec.street or "").strip(), (rec.number or "").strip()
    core = f"{number} {street}" if (rec.country or "").upper() in _EN_ORDER else f"{street} {number}"
    tail = " ".join(p for p in (rec.postcode, rec.city) if p)
    address = f"{core}, {tail}" if tail else core
    return dataclasses.replace(rec, address=address)


# --------------------------------------------------------------------------- #
# OpenAddresses
# --------------------------------------------------------------------------- #

def build_oa(only: set[str] | None) -> list[dict]:
    built = []
    regions = [(p, OPENADDRESSES_PRESETS[p].slug, iso, OPENADDRESSES_PRESETS[p].depot_city,
                OPENADDRESSES_PRESETS[p].depot_country, OPENADDRESSES_PRESETS[p].depot_lat,
                OPENADDRESSES_PRESETS[p].depot_lon) for p, iso in OA_REGIONS.items()]
    regions += [(k, *v) for k, v in OA_EXTRA.items()]
    for preset, slug, iso, city, country, dlat, dlon in regions:
        if only and iso not in only and preset not in only:
            continue
        cache = OA_CACHE / f"{slug.replace('/', '_')}.geojson.gz"
        if not cache.is_file():
            print(f"  - {preset}: sin cache {cache.name}", flush=True)
            continue
        started = time.time()
        rng = random.Random(SEED)
        data = cache.read_bytes()
        # reservorio grande primero: los countrywide traen el pais entero
        pool: list[dict] = []
        seen = 0
        for feat in iter_geojson_features(data):
            if not (feat.get("street") and feat.get("number")):
                continue
            seen += 1
            if len(pool) < 20000:
                pool.append(feat)
            else:
                j = rng.randrange(seen)
                if j < 20000:
                    pool[j] = feat
        records = [r for r in _records_from_rows(pool, source_label=f"openaddresses:{slug}",
                                                 country=iso, styles=("oa_default",))
                   if r.has_coords and r.street and r.number]
        near = [r for r in records if km(dlat, dlon, r.lat, r.lng) <= SAMPLE_RADIUS_KM]
        if len(near) < BASE_PER_REGION:
            lats = sorted(r.lat for r in records)
            lons = sorted(r.lng for r in records)
            dlat, dlon = lats[len(lats) // 2], lons[len(lons) // 2]
            near = [r for r in records if km(dlat, dlon, r.lat, r.lng) <= SAMPLE_RADIUS_KM]
            note_center = " Depot centrado en la mediana de los datos (el dataset no cubre la ciudad del preset)."
        else:
            note_center = ""
        rng.shuffle(near)
        bases = [_street_number_clean(dataclasses.replace(r, city=r.city or city))
                 for r in near[:BASE_PER_REGION]]
        if not bases:
            print(f"  - {preset}: 0 direcciones con calle+altura cerca del depot", flush=True)
            continue
        rows = expand(bases, seed=SEED)
        name = f"oa_{iso.lower()}_{preset}.json"
        write_corpus(OUT / name, source="openaddresses", iso=iso, city=city,
                     country=country, depot=(dlat, dlon), rows=rows,
                     note=f"OpenAddresses {slug}, {len(bases)} direcciones a <= {SAMPLE_RADIUS_KM:.0f} km del depot.{note_center}")
        built.append({"file": name, "iso": iso, "city": city, "country": country,
                      "depot": (dlat, dlon), "source": "openaddresses", "rows": len(rows)})
        print(f"  ✓ {preset}: {len(bases)} bases → {len(rows)} filas ({time.time() - started:.0f}s)", flush=True)
    return built


# --------------------------------------------------------------------------- #
# OSM
# --------------------------------------------------------------------------- #

def build_osm(only: set[str] | None) -> list[dict]:
    from smart_import.config import Config
    from smart_import.geocoding.extract import ensure_geocode_index_from_config

    cfg = dataclasses.replace(Config.from_env(), extract_margin_km=INDEX_MARGIN_KM)
    built = []
    for iso, (city, country, lat, lon) in OSM_CITIES.items():
        if only and iso not in only:
            continue
        started = time.time()
        try:
            ready = ensure_geocode_index_from_config(
                cfg, lat=lat, lon=lon, bbox=None, zone_hint=f"{country} {city}")
        except Exception as exc:          # sin PBF / osmium: se reporta y se sigue
            print(f"  ✗ {iso} {city}: indice ({type(exc).__name__}: {str(exc)[:120]})", flush=True)
            continue
        conn = sqlite3.connect(f"file:{ready.path}?mode=ro", uri=True)
        dlat = SAMPLE_RADIUS_KM / 111.0
        dlon = SAMPLE_RADIUS_KM / (111.0 * max(0.2, math.cos(math.radians(lat))))
        rows = conn.execute(
            "SELECT lat, lon, street, house_number, city, postcode FROM places"
            " WHERE street IS NOT NULL AND street <> '' AND house_number IS NOT NULL"
            " AND house_number <> '' AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()
        conn.close()
        rng = random.Random(SEED)
        # una direccion por calle: 40 alturas de la misma avenida no son 40 casos
        by_street: dict[str, list] = {}
        for r in rows:
            if km(lat, lon, r[0], r[1]) <= SAMPLE_RADIUS_KM and any(ch.isdigit() for ch in r[3]):
                by_street.setdefault(r[2], []).append(r)
        streets = sorted(by_street)
        rng.shuffle(streets)
        bases: list[CorpusRecord] = []
        for street in streets[:BASE_PER_REGION]:
            plat, plon, st, hn, c, pc = rng.choice(by_street[street])
            rec = CorpusRecord(address="", lat=plat, lng=plon, street=st, number=hn,
                               city=c or city, postcode=pc, country=iso,
                               source=f"osm-addr:{Path(ready.path).name}")
            bases.append(_street_number_clean(rec))
        if len(bases) < 5:
            print(f"  ✗ {iso} {city}: {len(rows)} nodos addr, {len(bases)} calles (OSM sin direcciones)", flush=True)
            continue
        out_rows = expand(bases, seed=SEED)
        name = f"osm_{iso.lower()}_{city.lower().replace(' ', '_').replace('ã', 'a').replace('á', 'a').replace('é', 'e').replace('ó', 'o')}.json"
        write_corpus(OUT / name, source="osm-addr", iso=iso, city=city, country=country,
                     depot=(lat, lon), rows=out_rows,
                     note=(f"Nodos addr:* del indice {Path(ready.path).name} a <= {SAMPLE_RADIUS_KM:.0f} km,"
                           f" una direccion por calle. La fila limpia es optimista: mide el ruido."))
        built.append({"file": name, "iso": iso, "city": city, "country": country,
                      "depot": (lat, lon), "source": "osm-addr", "rows": len(out_rows),
                      "index": Path(ready.path).name})
        print(f"  ✓ {iso} {city}: {len(rows)} nodos, {len(bases)} bases → {len(out_rows)} filas"
              f" [{Path(ready.path).name}] ({time.time() - started:.0f}s)", flush=True)
    return built


def update_manifest() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    suites = [s for s in manifest["suites"] if not s.get("generated")]
    for path in sorted(OUT.glob("o*_*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        iso = doc["country"]
        depot = doc["depot"]
        suite = {
            "id": path.stem,
            "corpus": f"corpora/{path.name}",
            "depot": {"lat": depot["lat"], "lon": depot["lon"], "city": depot["city"],
                      "country": depot["country"]},
            "limits": {"fast": 90},
            "tags": ["noise", doc["source"].split("-")[0], iso.lower()],
            "generated": True,
        }
        if doc["source"] == "osm-addr":
            suite["index_margin_km"] = INDEX_MARGIN_KM
        suites.append(suite)
    manifest["suites"] = suites
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  manifest: {len(suites)} suites")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=("oa", "osm", "all"), default=None)
    ap.add_argument("--only", help="ISO2 o preset separados por coma")
    ap.add_argument("--manifest", action="store_true", help="regenerar las suites generadas del manifest")
    args = ap.parse_args()
    only = {s.strip() for s in (args.only or "").split(",") if s.strip()} or None
    if args.source in ("oa", "all"):
        build_oa(only)
    if args.source in ("osm", "all"):
        build_osm(only)
    if args.manifest:
        update_manifest()


if __name__ == "__main__":
    main()
