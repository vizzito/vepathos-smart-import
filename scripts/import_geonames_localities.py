#!/usr/bin/env python3
"""Genera locality_expand_geonames.json desde GeoNames (cities15000).

¿Por que NO allCountries?
  allCountries.zip ≈ 400 MB comprimido / ~1.5 GB texto. No hace falta:
  cities15000.zip ≈ 2–3 MB y cubre ciudades >15k hab. + capitales admin.

¿Ayuda de verdad?
  Si: cuando el texto trae ciudad sin pais ("…, Lyon", "…, Mumbai").
  No tanto: si el depot ya inyecta city/country (flujo tipico con mapa).
  El JSON curado (CABA, CDMX, …) sigue ganando para aliases especiales.

Uso:
  python scripts/import_geonames_localities.py
  python scripts/import_geonames_localities.py --min-population 50000 --dry-run

Salida:
  smart_import/resources/locality_expand_geonames.json   (~1–2 MB tipico)
Cache (gitignore bajo data/):
  data/geonames/cities15000.zip   — descarga una vez
  data/geonames/cities15000.txt   — se extrae una vez desde el zip; reuso en corridas
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import unicodedata
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ROOT / "smart_import" / "resources"
ISO_FILE = RESOURCES / "iso3166_alpha2.json"
OUT_FILE = RESOURCES / "locality_expand_geonames.json"
CACHE_DIR = ROOT / "data" / "geonames"
GEONAMES_URL = "https://download.geonames.org/export/dump/cities15000.zip"
ZIP_NAME = "cities15000.zip"
TXT_NAME = "cities15000.txt"

# Palabras demasiado cortas / ambiguas como cue (falso positivo en direcciones).
_DENY_CUES = frozenset({
    "la", "el", "los", "las", "de", "del", "san", "santa", "new", "old",
    "port", "fort", "east", "west", "north", "south", "city", "town",
    "rio", "sur", "norte", "este", "oeste", "le", "les", "des", "du",
    "st", "ste", "bay", "lake", "park", "spring", "springs", "hill", "hills",
    "union", "center", "centre", "colonia", "pueblo", "villa",
})


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def load_iso() -> dict[str, str]:
    raw = json.loads(ISO_FILE.read_text(encoding="utf-8"))
    return {str(k).upper(): str(v) for k, v in raw.items()}


def download_zip(dest: Path, *, force: bool = False) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        print(f"zip cache hit: {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest
    print(f"descargando {GEONAMES_URL} …")
    urllib.request.urlretrieve(GEONAMES_URL, dest)
    print(f"ok: {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


def extract_txt(zip_path: Path, txt_path: Path, *, force: bool = False) -> Path:
    """Extrae cities15000.txt una sola vez; no reabre el zip en corridas siguientes."""
    if txt_path.exists() and not force:
        print(f"txt cache hit: {txt_path} ({txt_path.stat().st_size / 1e6:.1f} MB)")
        return txt_path
    print(f"extrayendo {txt_path.name} desde {zip_path.name} …")
    with zipfile.ZipFile(zip_path) as zf:
        member = next(n for n in zf.namelist() if n.endswith(".txt"))
        with zf.open(member) as src, txt_path.open("wb") as dst:
            dst.write(src.read())
    print(f"ok: {txt_path} ({txt_path.stat().st_size / 1e6:.1f} MB)")
    return txt_path


def resolve_cities_txt(
    *,
    zip_path: Path | None,
    txt_path: Path,
    force_download: bool,
    force_extract: bool,
) -> Path:
    """Prioridad: .txt ya extraído → zip local → descarga + extract."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    txt_path.parent.mkdir(parents=True, exist_ok=True)

    if txt_path.exists() and not force_extract and not force_download:
        print(f"txt cache hit: {txt_path} ({txt_path.stat().st_size / 1e6:.1f} MB)")
        return txt_path

    zip_dest = zip_path if zip_path is not None else (CACHE_DIR / ZIP_NAME)
    if zip_path is None:
        download_zip(zip_dest, force=force_download)
    elif not zip_dest.exists():
        raise FileNotFoundError(f"no existe el zip: {zip_dest}")
    else:
        print(f"zip cache hit: {zip_dest} ({zip_dest.stat().st_size / 1e6:.1f} MB)")

    return extract_txt(zip_dest, txt_path, force=True)


def iter_cities(txt_path: Path):
    with txt_path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        for row in reader:
            if len(row) < 15:
                continue
            # geonameid name asciiname alternatenames lat lon fclass fcode
            # country cc2 admin1 admin2 admin3 admin4 population …
            yield {
                "name": row[1],
                "ascii": row[2],
                "alts": row[3],
                "fcode": row[7],
                "country": row[8].upper(),
                "population": int(row[14] or 0),
            }


def cue_ok(cue: str, *, min_len: int) -> bool:
    c = fold(cue)
    if len(c) < min_len:
        return False
    if c in _DENY_CUES:
        return False
    if not any(ch.isalpha() for ch in c):
        return False
    return True


def build_expansions(
    cities,
    iso: dict[str, str],
    *,
    min_population: int,
    min_cue_len: int,
    max_alts: int,
) -> list[dict]:
    # Agrupar por (country, fold(ascii|name)) para no duplicar
    by_key: dict[tuple[str, str], dict] = {}
    for city in cities:
        if city["population"] < min_population:
            continue
        country_name = iso.get(city["country"])
        if not country_name:
            continue
        primary = city["ascii"] or city["name"]
        if not cue_ok(primary, min_len=min_cue_len):
            continue
        key = (city["country"], fold(primary))
        entry = by_key.get(key)
        if entry is None:
            cues = []
            for raw in (city["ascii"], city["name"]):
                if raw and cue_ok(raw, min_len=min_cue_len) and fold(raw) not in {fold(c) for c in cues}:
                    cues.append(raw.strip())
            # pocos alternates largos (evita ruido)
            alts = [a.strip() for a in (city["alts"] or "").split(",") if a.strip()]
            for alt in alts:
                if len(cues) >= 1 + max_alts:
                    break
                if cue_ok(alt, min_len=max(min_cue_len, 5)) and fold(alt) not in {fold(c) for c in cues}:
                    cues.append(alt)
            by_key[key] = {
                "cues": cues,
                "tokens": [country_name],
                "population": city["population"],
            }
        else:
            entry["population"] = max(entry["population"], city["population"])

    # Orden: mas pobladas primero (mejor cue gana si hay solape en maximize)
    rows = sorted(by_key.values(), key=lambda r: -r["population"])
    return [{"cues": r["cues"], "tokens": r["tokens"]} for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-population", type=int, default=50_000,
                        help="Piso de poblacion GeoNames (default 50000; usar 15000 para mas cobertura)")
    parser.add_argument("--min-cue-len", type=int, default=4,
                        help="Largo minimo del cue (default 4; evita 'La', 'Rio')")
    parser.add_argument("--max-alts", type=int, default=2,
                        help="Alternates GeoNames por ciudad (default 2)")
    parser.add_argument(
        "--zip",
        type=Path,
        default=None,
        help=f"Ruta a un cities15000.zip ya descargado (default: {CACHE_DIR / ZIP_NAME})",
    )
    parser.add_argument(
        "--txt",
        type=Path,
        default=CACHE_DIR / TXT_NAME,
        help=f"Cache del .txt extraído (default: {CACHE_DIR / TXT_NAME})",
    )
    parser.add_argument("--force-download", action="store_true",
                        help="Re-descarga el zip (implica re-extraer el .txt)")
    parser.add_argument("--force-extract", action="store_true",
                        help="Re-extrae el .txt desde el zip aunque ya exista")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-o", "--output", type=Path, default=OUT_FILE)
    args = parser.parse_args(argv)

    if not ISO_FILE.exists():
        print(f"falta {ISO_FILE}", file=sys.stderr)
        return 1

    iso = load_iso()
    try:
        txt_path = resolve_cities_txt(
            zip_path=args.zip,
            txt_path=args.txt,
            force_download=args.force_download,
            force_extract=args.force_extract,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    cities = list(iter_cities(txt_path))
    print(f"ciudades leidas: {len(cities)}")

    expansions = build_expansions(
        cities, iso,
        min_population=args.min_population,
        min_cue_len=args.min_cue_len,
        max_alts=args.max_alts,
    )
    payload = {
        "version": 1,
        "_comment": (
            "Generado por scripts/import_geonames_localities.py. "
            "No editar a mano — regenerar el script. "
            "Se fusiona con locality_expand.json (curado) al cargar."
        ),
        "source": "geonames cities15000",
        "url": GEONAMES_URL,
        "min_population": args.min_population,
        "min_cue_len": args.min_cue_len,
        "expansions": expansions,
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    size_mb = len(raw.encode("utf-8")) / 1e6
    print(f"expansions: {len(expansions)}  json≈{size_mb:.2f} MB")

    if args.dry_run:
        print("dry-run: no se escribio archivo")
        print("ejemplos:", expansions[:3])
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(raw + "\n", encoding="utf-8")
    print(f"escrito: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
