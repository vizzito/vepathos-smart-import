#!/usr/bin/env python3
"""Genera corpus de direcciones para geocode-truth / traps.

OpenAddresses (2026+): batch.openaddresses.io + token OPENADDRESSES_TOKEN.
Las URLs legacy data.openaddresses.io/runs/…/countrywide.zip dan 404.

Uso:
  export OPENADDRESSES_TOKEN=oa.xxxx   # batch.openaddresses.io → Profile → API token

  python scripts/generate_address_corpus.py --list-presets
  python scripts/generate_address_corpus.py --preset argentina -n 50 --country AR

  # ~30 países de los idiomas del parser (100 dirs c/u; salta si OA no tiene o es enorme)
  N=100 && python scripts/generate_address_corpus.py --pack languages -n $N

  # Un solo archivo mezclado (31 países × N filas)
  N=100 && python scripts/generate_address_corpus.py --pack languages --mix -n $N \\
      --out "examples/geocode-truth/generated/languages_mix_n${N}.json"

  # Mix + ruido humano (misma lat/lng; no pisa el mix limpio)
  N=100 && python scripts/generate_address_corpus.py --pack languages --mix -n $N \\
      --noise-level 5 --noise-baseline --noise-permutations 4 \\
      --out "examples/geocode-truth/generated/languages_mix_n${N}.json"

  # Otra muestra (100 dirs distintas por país; imprime el seed)
  N=100 && python scripts/generate_address_corpus.py --pack languages --mix --new-sample -n $N

  # Sin token / sin red: fixture local CABA (15 dirs con coords)
  python scripts/generate_address_corpus.py --preset argentina --fixture -n 50

  # Varias redacciones
  python scripts/generate_address_corpus.py --preset argentina --fixture \\
      --styles oa_default,us,eu -n 10

  # Direcciones humanas / incompletas (misma lat/lng verificada)
  python scripts/generate_address_corpus.py --preset argentina --fixture \\
      --noise-level 5 --noise-cumulative --noise-permutations 4

  # Medir error vs verdad (genera el JSON si no existe)
  .venv/bin/python -m smart_import geocode-accuracy \\
      --truth examples/geocode-truth/generated/argentina_fixture_n150.json \\
      --depot-city CABA --origin-lat -34.598 --origin-lon -58.416
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_dotenv_token() -> None:
    """OPENADDRESSES_TOKEN desde .env si no está en el entorno."""
    if os.environ.get("OPENADDRESSES_TOKEN"):
        return
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() != "OPENADDRESSES_TOKEN":
            continue
        val = val.strip().strip("'\"")
        if val:
            os.environ["OPENADDRESSES_TOKEN"] = val
        return

from smart_import.tools.address_noise import expand_records_with_noise  # noqa: E402
from smart_import.tools.address_corpus import (  # noqa: E402
    ADDRESS_STYLES,
    LIBPOSTAL_DEFAULT_TSV,
    LOCAL_OA_FIXTURES,
    OPENADDRESSES_PRESETS,
    OPENADDRESSES_TOKEN_ENV,
    PRESET_PACKS,
    OpenAddressesError,
    load_openaddresses_batch,
    load_openaddresses_fixture,
    load_openaddresses_zip,
    load_cached_or_download,
    load_parser_text_corpus,
    mix_corpus_records,
    resolve_sample_seed,
    sample_filename_tag,
    slugify,
    with_corpus_suffixes,
    suggest_accuracy_flags,
    tag_record_preset,
    token_help_message,
    write_csv_corpus,
    write_json_corpus,
)


def _parse_styles(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ("oa_default",)
    styles = tuple(s.strip() for s in raw.split(",") if s.strip())
    unknown = [s for s in styles if s not in ADDRESS_STYLES]
    if unknown:
        raise SystemExit(
            f"estilos desconocidos: {unknown}. Validos: {', '.join(ADDRESS_STYLES)}")
    return styles


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_token()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=("openaddresses", "parser_text"),
                        default="openaddresses")
    parser.add_argument("--preset", choices=sorted(OPENADDRESSES_PRESETS),
                        help="fuente OpenAddresses (slug batch API)")
    parser.add_argument(
        "--pack", choices=sorted(PRESET_PACKS),
        help="generar todos los presets del pack (languages ≈ 30 países)")
    parser.add_argument(
        "--mix", action="store_true",
        help="juntar el pack en un solo JSON/CSV mezclado (implica --pack languages)")
    parser.add_argument("--slug", help="slug OA custom (pisa preset), ej. ar/c/city_of_buenos_aires")
    parser.add_argument("--url", help="ZIP legacy OpenAddresses (si aún tenés uno)")
    parser.add_argument("--token", help=f"Bearer token (default: ${OPENADDRESSES_TOKEN_ENV})")
    parser.add_argument("--fixture", action="store_true",
                        help="fixture CSV local (sin token ni red)")
    parser.add_argument("--allow-large", action="store_true",
                        help="permitir descargas gzip > max del preset")
    parser.add_argument("-n", "--sample-size", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42,
                        help="muestra reproducible (default 42 = siempre las mismas 100)")
    parser.add_argument(
        "--new-sample", action="store_true",
        help="otra muestra aleatoria (imprime el seed; repetí con --seed N)")
    parser.add_argument("--country", help="ISO/pais en salida (default: del preset)")
    parser.add_argument("--styles", default="oa_default",
                        help=f"redacciones: {', '.join(ADDRESS_STYLES)}")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "data" / "openaddresses")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--list-presets", action="store_true")
    parser.add_argument("--noise-level", type=int, choices=range(1, 7),
                        metavar="{1-6}",
                        help="ruido humano: 1=mínimo … 5=permutaciones casi completas, 6=planilla de despacho")
    parser.add_argument("--noise-cumulative", action="store_true",
                        help="generar niveles 1..N (no solo el nivel pedido)")
    parser.add_argument("--noise-rate", type=float, default=1.0,
                        help="fracción de filas base a expandir (0–1], default 1.0")
    parser.add_argument("--noise-permutations", type=int, default=4,
                        help="máx. permutaciones por nivel (3–5)")
    parser.add_argument("--noise-baseline", action="store_true",
                        help="incluir fila limpia original (noise_level=0)")
    args = parser.parse_args(argv)

    if args.list_presets:
        _print_presets()
        return 0

    args.seed = resolve_sample_seed(args.seed, new_sample=args.new_sample)
    if args.new_sample:
        print(f"seed={args.seed}  (repetir esta muestra: --seed {args.seed})")

    if args.mix and not args.pack:
        args.pack = "languages"
    if args.pack and args.preset:
        parser.error("usá --pack/--mix o --preset, no los dos")

    if args.pack:
        return _run_pack(args, parser)

    return _run_one(args, parser)


def _print_presets() -> None:
    packs_of: dict[str, list[str]] = {name: [] for name in OPENADDRESSES_PRESETS}
    for pack_name, members in PRESET_PACKS.items():
        if pack_name == "all":
            continue
        for member in members:
            packs_of.setdefault(member, []).append(pack_name)

    print(f"{'preset':16} {'cc':3} {'lang':4} slug")
    for name, preset in sorted(OPENADDRESSES_PRESETS.items()):
        fix = " [fixture]" if name in LOCAL_OA_FIXTURES else ""
        tags = ",".join(packs_of.get(name) or [])
        extra = f"  [{tags}]" if tags else ""
        print(f"{name:16} {(preset.country or ''):3} {(preset.lang or ''):4} "
              f"{preset.slug}{fix}{extra}")
        if preset.note:
            depot = ""
            if preset.depot_city:
                depot = f"  depot={preset.depot_city}"
            print(f"{'':16} # {preset.note}{depot}")

    print("\nPacks:")
    for pack_name, members in PRESET_PACKS.items():
        countries = {OPENADDRESSES_PRESETS[n].country for n in members}
        print(f"  {pack_name:12} {len(members)} fuentes / {len(countries)} países")
    print("\nOA no tiene (entre otros): GB, IE, IN, PE.")
    print(f"parser_text      {LIBPOSTAL_DEFAULT_TSV}")
    print(f"Token: {OPENADDRESSES_TOKEN_ENV}  →  https://batch.openaddresses.io/profile")
    print("\nN=100 && python scripts/generate_address_corpus.py --pack languages --mix --new-sample -n $N")


def _run_pack(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    names = PRESET_PACKS[args.pack]
    generated = ROOT / "examples" / "geocode-truth" / "generated"
    n_tag = sample_filename_tag(
        args.sample_size, args.seed, noise_level=args.noise_level)
    if args.mix:
        if args.out and args.out.suffix.lower() in {".json", ".csv"}:
            out_mix = with_corpus_suffixes(
                args.out, seed=args.seed, noise_level=args.noise_level)
        else:
            base = args.out if args.out else generated
            out_mix = base / f"{args.pack}_mix_{n_tag}.json"
        out_dir = out_mix.parent
    else:
        out_dir = args.out if args.out else generated
        if args.out and args.out.suffix.lower() in {".json", ".csv"}:
            out_dir = args.out.parent
        out_mix = None
    out_dir.mkdir(parents=True, exist_ok=True)

    ok: list[str] = []
    failed: list[tuple[str, str]] = []
    groups: list[list] = []
    mode = "mix → 1 archivo" if args.mix else "1 archivo por país"
    print(f"Pack {args.pack}: {len(names)} fuentes, n={args.sample_size} ({mode})")
    print(f"Salida: {out_mix if args.mix else out_dir}")

    for name in names:
        one = argparse.Namespace(**vars(args))
        one.pack = None
        one.mix = False
        one.preset = name
        one.country = args.country or OPENADDRESSES_PRESETS[name].country
        one.out = out_dir / f"{name}_{n_tag}.json"
        one.csv = one.out.with_suffix(".csv")
        print(f"\n===== {name} ({one.country}) =====")
        try:
            records = _load_records(one, parser)
        except (OpenAddressesError, urllib.error.HTTPError, SystemExit) as exc:
            records = None
            print(f"Error: {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — el pack sigue con el resto
            records = None
            print(f"Error: {exc}", file=sys.stderr)
        if not records:
            failed.append((name, str(one.out.name)))
            continue
        tagged = [tag_record_preset(rec, name) for rec in records]
        ok.append(name)
        if args.mix:
            groups.append(tagged)
            print(f"  +{len(tagged)} filas")
        else:
            _write_corpus(one, tagged,
                          note=getattr(one, "_note", None) or _pack_member_note(one, tagged),
                          url=getattr(one, "_url", None))

    if args.mix and groups:
        mixed = mix_corpus_records(groups, seed=args.seed)
        countries = sorted({
            OPENADDRESSES_PRESETS[n].country or n for n in ok
        })
        noise_bit = ""
        if args.noise_level:
            noise_bit = f", noise={args.noise_level}"
            if args.noise_baseline:
                noise_bit += "+baseline"
        note = (
            f"Mix {args.pack}: {len(ok)} países × hasta {args.sample_size} dirs "
            f"(seed={args.seed}, barajado{noise_bit}). Fallidos: {len(failed)}."
        )
        mix_args = argparse.Namespace(**vars(args))
        mix_args.preset = None
        mix_args.out = out_mix
        mix_args.csv = out_mix.with_suffix(".csv")
        _write_corpus(mix_args, mixed, note=note, url=None,
                      pack=args.pack, countries=countries, mixed=True)

    print(f"\nPack {args.pack}: {len(ok)} ok, {len(failed)} fallidos")
    if ok:
        print("  ok: " + ", ".join(ok))
    if failed:
        print("  fallidos: " + ", ".join(n for n, _ in failed))
        print("  (OA sin fuente, token, o gzip > limite del preset)")
    return 0 if ok and not failed else (0 if ok else 1)


def _pack_member_note(args: argparse.Namespace, records: list) -> str:
    return (
        f"Muestra aleatoria (seed={args.seed}) preset={args.preset}. "
        f"{len(records)} filas."
    )


def _load_records(args: argparse.Namespace, parser: argparse.ArgumentParser):
    """Baja/muestrea filas. None si no hay datos. Aplica --noise-level."""
    styles = _parse_styles(args.styles)
    token = args.token or os.environ.get(OPENADDRESSES_TOKEN_ENV)
    job_id: int | None = None
    url = ""
    note = ""
    records = []

    if args.source == "parser_text":
        tsv_source = args.url or LIBPOSTAL_DEFAULT_TSV
        records = load_parser_text_corpus(tsv_source, sample_size=args.sample_size, seed=args.seed)
        url = str(tsv_source)
        note = "Solo texto (TSV). No usar con geocode-accuracy."
    elif args.fixture:
        if not args.preset:
            parser.error("--fixture requiere --preset")
        records = load_openaddresses_fixture(
            args.preset, sample_size=args.sample_size, seed=args.seed,
            country=args.country or OPENADDRESSES_PRESETS[args.preset].country,
            styles=styles)
        url = str(LOCAL_OA_FIXTURES.get(args.preset, ""))
        note = f"Fixture local (seed={args.seed}). Sin OpenAddresses Batch."
    elif args.url:
        url = args.url
        label = slugify(Path(url).name or "custom")
        cache_path = None if args.no_cache else args.cache_dir / f"{slugify(label)}.zip"
        print(f"Descargando ZIP legacy {label}…")
        try:
            data = load_cached_or_download(url, cache_path, token=token)
        except urllib.error.HTTPError as exc:
            print(f"HTTP {exc.code}: {exc.reason}", file=sys.stderr)
            if exc.code == 404:
                print("Las URLs data.openaddresses.io/runs/… ya no existen.", file=sys.stderr)
                print(token_help_message(), file=sys.stderr)
            return None
        records = load_openaddresses_zip(
            data, sample_size=args.sample_size, seed=args.seed,
            source_label=f"openaddresses:{label}",
            country=args.country, styles=styles)
        note = f"Muestra desde ZIP legacy (seed={args.seed})."
    else:
        preset_meta = OPENADDRESSES_PRESETS.get(args.preset) if args.preset else None
        if args.slug:
            slug = args.slug
        elif preset_meta:
            slug = preset_meta.slug
        else:
            parser.error("openaddresses requiere --preset, --slug, --url o --fixture")

        country = args.country or (preset_meta.country if preset_meta else None)
        max_gz = preset_meta.max_gz_mb if preset_meta else 80.0

        cache_path = None
        if not args.no_cache:
            cache_path = args.cache_dir / f"{slugify(slug)}.geojson.gz"

        print(f"OpenAddresses Batch: {slug}…")
        try:
            records, url, job_id = load_openaddresses_batch(
                slug, sample_size=args.sample_size, seed=args.seed, token=token,
                country=country, styles=styles, cache_path=cache_path,
                max_gz_mb=max_gz, allow_large=args.allow_large)
        except OpenAddressesError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            if args.preset and args.preset in LOCAL_OA_FIXTURES:
                print(f"\nAlternativa sin token:\n"
                      f"  python scripts/generate_address_corpus.py --preset {args.preset} --fixture",
                      file=sys.stderr)
            return None
        except urllib.error.HTTPError as exc:
            print(f"HTTP {exc.code}: {exc.reason}", file=sys.stderr)
            if exc.code in (401, 403):
                print("\nToken inválido o faltante.", file=sys.stderr)
                print(token_help_message(), file=sys.stderr)
            elif exc.code == 400:
                print("\nDescarga OA falló (redirect/CDN). Actualizá el repo "
                      "o probá: pip install -e '.[queue]' y reintentá.",
                      file=sys.stderr)
            return None

        if cache_path and cache_path.is_file():
            print(f"  cache: {cache_path} ({cache_path.stat().st_size // 1024} KiB)")
        print(f"  job={job_id}")
        note = (
            f"Muestra aleatoria (seed={args.seed}) desde batch.openaddresses.io job {job_id}. "
            f"Estilos: {', '.join(styles)}."
        )

    if not records:
        print("No se obtuvieron filas validas.", file=sys.stderr)
        return None

    if args.noise_level:
        if args.source == "parser_text":
            print("--noise-level requiere coords (openaddresses / --fixture).",
                  file=sys.stderr)
            return None
        base_count = len(records)
        records = expand_records_with_noise(
            records,
            args.noise_level,
            seed=args.seed,
            cumulative=args.noise_cumulative,
            max_permutations=max(1, args.noise_permutations),
            rate=args.noise_rate,
            include_baseline=args.noise_baseline,
        )
        note = (
            f"{note} Ruido humano nivel 1–{args.noise_level} "
            f"({'acumulativo' if args.noise_cumulative else 'solo nivel'}), "
            f"rate={args.noise_rate}, permutations<={args.noise_permutations}. "
            f"Base={base_count} → {len(records)} variantes."
        )

    args._url = url
    args._note = note
    return records


def _write_corpus(args: argparse.Namespace, records, *, note: str, url: str | None,
                  pack: str | None = None, countries: list[str] | None = None,
                  mixed: bool = False) -> None:
    seed = getattr(args, "seed", None)
    noise_level = getattr(args, "noise_level", None)
    if args.out:
        out_json = with_corpus_suffixes(
            args.out, seed=seed, noise_level=noise_level)
    else:
        n_tag = (
            sample_filename_tag(args.sample_size, seed, noise_level=noise_level)
            if seed is not None else f"n{len(records)}")
        out_json = (
            ROOT / "examples" / "geocode-truth" / "generated"
            / f"{args.preset or 'corpus'}_{n_tag}.json")
    write_json_corpus(records, out_json, note=note, preset=args.preset, url=url,
                      pack=pack, countries=countries, seed=getattr(args, "seed", None))

    out_csv = args.csv
    if out_csv is None and args.source == "openaddresses":
        out_csv = out_json.with_suffix(".csv")
    if out_csv is not None and records and records[0].has_coords:
        write_csv_corpus(records, out_csv)

    with_coords = sum(1 for r in records if r.has_coords)
    swapped = sum(1 for r in records if r.extra.get("coord_fix") == "swapped_lat_lng")
    print(f"\nListo: {len(records)} filas ({with_coords} con coords)")
    if swapped:
        print(f"  aviso: {swapped}/{with_coords} tenían lat/lng invertidos "
              f"(fuera del país; se swapearon). Fuente OA, no el PBF.")
    print(f"  JSON: {out_json}")
    if out_csv is not None and records and records[0].has_coords:
        print(f"  CSV:  {out_csv}")
    print("\nMuestra:")
    for i, rec in enumerate(records[:5], start=1):
        coords = f"{rec.lat:.5f}, {rec.lng:.5f}" if rec.has_coords else "sin coords"
        country = rec.country or ""
        print(f"  {i:02d} [{rec.style}] {country} {rec.address}  ({coords})")

    if not with_coords:
        return
    print("\nMedir precision (metros de error vs verdad):")
    print(f"  .venv/bin/python -m smart_import geocode-accuracy --truth {out_json} \\")
    if mixed:
        print("      # mix: geocode-accuracy parte por país (no pases --depot-city)")
        return
    hint = suggest_accuracy_flags(records, preset=args.preset)
    if hint:
        print(f"      {hint}")
    else:
        print("      --origin-lat LAT --origin-lon LON")


def _run_one(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    records = _load_records(args, parser)
    if not records:
        return 1
    note = getattr(args, "_note", "")
    url = getattr(args, "_url", None)
    _write_corpus(args, records, note=note, url=url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
