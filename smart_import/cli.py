"""CLI de Vepathos Smart Import."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from .config import Config
from .mapping import build_mapper
from .readers import read_any
from .schemas import TargetSchema

app = typer.Typer(add_completion=False, help="Vepathos Smart Import - normaliza archivos de entregas.")

#: El shell parte 'San Francisco' en dos argv; Typer/Click solo toma el primero.
_SPACED_OPTIONS = (
    "--depot-city",
    "--depot-region",
    "--depot-country",
    "--depot-address",
)


def join_spaced_option_values(argv: list[str]) -> list[str]:
    """Une palabras de un option hasta el próximo flag.

    `--depot-city san francisco --origin-lat 37` → city='san francisco'.
    Las comillas siguen funcionando (`--depot-city "San Francisco"`).
    """
    flags = set(_SPACED_OPTIONS)
    out: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        token = argv[i]
        eq = next((flag for flag in flags if token.startswith(f"{flag}=")), None)
        if eq is not None:
            first = token[len(eq) + 1 :]
            i += 1
            parts = [first] if first else []
            while i < n and not argv[i].startswith("-"):
                parts.append(argv[i])
                i += 1
            out.append(f"{eq}={' '.join(parts)}" if parts else token)
            continue
        if token in flags:
            out.append(token)
            i += 1
            parts: list[str] = []
            while i < n and not argv[i].startswith("-"):
                parts.append(argv[i])
                i += 1
            if parts:
                out.append(" ".join(parts))
            continue
        out.append(token)
        i += 1
    return out

DEFAULT_SCHEMA = "schemas/vepathos_flat_v1.json"


@app.callback()
def _root(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log paso a paso de cada etapa."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Solo errores."),
) -> None:
    """Los logs van a stderr, para que stdout quede limpio y pipeable."""
    from .logging_setup import setup
    setup(verbose=verbose, quiet=quiet)


def _echo_json(payload: dict) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


@app.command()
def inspect(
    file: Path = typer.Argument(..., exists=True, readable=True),
    sheet: str = typer.Option(None, help="Hoja a leer (Excel)."),
    rows: int = typer.Option(5, help="Filas de ejemplo a mostrar."),
) -> None:
    """Que hay en el archivo: formato, encoding, delimiter, hojas, columnas, tipos."""
    from .mapping.heuristics import ColumnProfile

    cfg = Config.from_env()
    table = read_any(file, max_rows=cfg.max_rows, sheet=sheet)
    m = table.meta

    columns = []
    for col in table.columns:
        p = ColumnProfile(table.column_values(col))
        columns.append({
            "name": col,
            "apparent_type": ("number" if p.numeric_frac >= 0.9 else
                              "empty" if p.n == 0 else "text"),
            "null_pct": round(100 * (1 - p.n / max(1, len(table))), 1),
            "distinct": p.distinct,
            "examples": p.texts[:3],
        })

    _echo_json({
        "path": str(file), "format": m.format, "size_bytes": m.size_bytes,
        "encoding": m.encoding, "delimiter": m.delimiter,
        "sheets": m.sheets, "sheet": m.sheet,
        "header_row": m.header_row, "preamble_rows": m.preamble_rows,
        "rows": len(table), "column_count": len(table.columns),
        "columns": columns, "sample": table.sample(rows), "notes": m.notes,
    })


@app.command()
def detect(
    input: Path = typer.Option(..., "--input", "-i", exists=True),
    schema: Path = typer.Option(DEFAULT_SCHEMA, "--schema", "-s", exists=True),
    sheet: str = typer.Option(None),
) -> None:
    """Mapping columna -> campo del schema, con confidence y metodo."""
    cfg = Config.from_env()
    table = read_any(input, max_rows=cfg.max_rows, sheet=sheet)
    result = build_mapper(cfg).detect(table, TargetSchema.load(schema))
    _echo_json(result.as_dict())


@app.command()
def normalize(
    input: Path = typer.Option(..., "--input", "-i", exists=True),
    schema: Path = typer.Option(DEFAULT_SCHEMA, "--schema", "-s", exists=True),
    output: Path = typer.Option(..., "--output", "-o"),
    emit: str = typer.Option("flat", help="flat, nested o 'flat,nested'."),
    mapping: Path = typer.Option(None, help="JSON {columna: campo} con el mapping confirmado."),
    phone_region: str = typer.Option(None, help="Region ISO para telefonos (AR, US, IN...)."),
    timezone: str = typer.Option(
        None, help="IANA TZ (ej. America/Argentina/Buenos_Aires). Ventanas → UTC.",
    ),
    diagnostics: bool = typer.Option(False, help="Agrega columnas row_status/row_issues."),
    derive_volume: bool = typer.Option(False, help="Calcular volume_cm3 desde LxWxH si falta."),
    sheet: str = typer.Option(None),
) -> None:
    """Convierte el archivo al formato Vepathos. NUNCA geocodifica."""
    from .extraction.tz import resolve_timezone
    from .pipeline import run_normalize

    overrides = json.loads(Path(mapping).read_text(encoding="utf-8")) if mapping else None
    result = run_normalize(
        input, schema, output,
        emit=tuple(e.strip() for e in emit.split(",") if e.strip()),
        manual_mapping=overrides, phone_region=phone_region,
        diagnostics=diagnostics, sheet=sheet, derive_volume=derive_volume,
        timezone=resolve_timezone(timezone),
    )
    r = result.report
    typer.echo(f"  {r['rows_input']} filas leidas -> {r['deliveries']} entregas, {r['packages']} bultos")
    typer.echo(f"  {r['valid_rows']} con coordenadas | {r['needs_geocode']} necesitan geocoding | "
               f"{r['invalid_rows']} invalidas")
    if r["rejected_coordinates"]:
        typer.echo(f"  {r['rejected_coordinates']} fila(s) con coordenadas rechazadas por rango invalido")
    if r["needs_review"]:
        typer.echo(f"  REVISAR: {len(r['ambiguous'])} columna(s) con mapping ambiguo")
    for w in r["warnings"]:
        typer.echo(f"  aviso: {w}")
    for kind, path in result.outputs.items():
        typer.echo(f"  {kind}: {path}")


def main() -> None:
    sys.argv = [sys.argv[0], *join_spaced_option_values(sys.argv[1:])]
    app()


@app.command("make-fixtures")
def make_fixtures(
    out: Path = typer.Option("fixtures", "--out", "-o"),
    rows: int = typer.Option(40, help="Filas por fixture chico."),
    sizes: str = typer.Option("", help="Tamanos extra para los de volumen: '1000,5000,10000'."),
) -> None:
    """Genera los archivos de prueba (no se versionan binarios)."""
    from .fixtures_gen import build_all

    parsed = tuple(int(s.strip()) for s in sizes.split(",") if s.strip())
    created = build_all(out, rows=rows, sizes=parsed)
    for path in created:
        typer.echo(f"  {path}")
    typer.echo(f"  {len(created)} fixtures generados en {out}/")


@app.command()
def benchmark(
    schema: Path = typer.Option(DEFAULT_SCHEMA, "--schema", "-s", exists=True),
    fixtures: Path = typer.Option("fixtures", "--fixtures"),
    sizes: str = typer.Option("1000,5000,10000", help="Tamanos a medir."),
    repeats: int = typer.Option(3, help="Corridas por archivo (se promedia)."),
    out: Path = typer.Option("benchmarks", "--out"),
) -> None:
    """Mide tiempo por etapa y memoria. Genera benchmarks/results.{csv,json}."""
    from .benchmark import format_table, run
    from .fixtures_gen import scale

    parsed = [int(s.strip()) for s in sizes.split(",") if s.strip()]
    files = []
    for n in parsed:
        label = f"{n // 1000}k" if n >= 1000 else str(n)
        path = fixtures / f"scale_{label}.xlsx"
        if not path.exists():
            typer.echo(f"  generando {path} ...")
            path = scale(fixtures, n)
        files.append(path)

    cfg = Config.from_env()
    typer.echo(f"  parser de direcciones: {cfg.address_parser}\n")
    results = run(files, schema, repeats=repeats, out_dir=out, config=cfg)
    typer.echo(format_table(results))
    typer.echo(f"\n  resultados en {out}/results.csv y {out}/results.json")


@app.command("benchmark-extraction")
def benchmark_extraction(
    corpus: Path = typer.Option("examples/free-text", "--corpus",
                                help="Directorio con los .txt de texto libre."),
    parsers: str = typer.Option("heuristic", "--parsers",
                                help="heuristic,libpostal,enhanced,hybrid (los que falten se saltan)."),
    repeats: int = typer.Option(3, help="Corridas por documento (se promedia)."),
    region: str = typer.Option("AR", "--region", help="Region ISO para telefonos."),
    out: Path = typer.Option("benchmarks", "--out"),
) -> None:
    """Compara estrategias de parser de direcciones: costo y accuracy.

    Todo el pipeline es determinístico, asi que `ai_calls` es siempre 0.
    """
    from .benchmark_extraction import format_table, run

    cfg = Config.from_env()
    names = tuple(p.strip() for p in parsers.split(",") if p.strip())
    typer.echo(f"  corpus: {corpus} | parsers: {', '.join(names)} | region: {region}")
    results = run(corpus=corpus, parsers=names, repeats=repeats, region=region,
                  out_dir=out, config=cfg)
    typer.echo(format_table(results))
    typer.echo(f"\n  resultados en {out}/extraction.json")


@app.command("build-geocoder-index")
def build_geocoder_index(
    pbf: Path = typer.Option(None, "--pbf", help="Ruta al .osm.pbf de origen."),
    output: Path = typer.Option(None, "--output", "-o", help="Destino .sqlite."),
    origin_lat: float = typer.Option(None, "--origin-lat", help="Elegir el PBF por punto."),
    origin_lon: float = typer.Option(None, "--origin-lon"),
    pbf_dir: Path = typer.Option(None, "--pbf-dir", help="Directorio de PBFs (o SMART_IMPORT_PBF_DIR)."),
    index_dir: Path = typer.Option(None, "--index-dir"),
    location_index: str = typer.Option("flex_mem", help="Indice de nodos de osmium."),
) -> None:
    """Construye el indice SQLite de direcciones desde un .osm.pbf.

    Se hace UNA vez por region y queda cacheado: geocodificar despues no vuelve
    a tocar el PBF.
    """
    from .geocoding.extract import ExtractError, ensure_geocode_index_from_config
    from .geocoding.osm_index import build

    cfg = Config.from_env()
    target_index_dir = index_dir or Path(cfg.index_dir)

    if pbf is None:
        root = pbf_dir or cfg.pbf_dir
        if not root:
            raise typer.BadParameter("indica --pbf, o --pbf-dir/SMART_IMPORT_PBF_DIR con --origin-lat/--origin-lon")
        try:
            ready = ensure_geocode_index_from_config(
                cfg, lat=origin_lat, lon=origin_lon,
                pbf_dir=root, index_dir=target_index_dir,
            )
        except (FileNotFoundError, ExtractError) as exc:
            typer.echo(f"  {exc}", err=True)
            raise typer.Exit(code=1)
        pbf = ready.entry.path
        output = output or ready.path
        extra = " (extract generado)" if ready.cut_extract else ""
        typer.echo(
            f"  PBF elegido: {pbf.name} (zona {ready.entry.zone}, "
            f"{ready.entry.size_bytes / 1e6:.1f} MB){extra}"
        )
        if ready.path.exists():
            if ready.built_index:
                typer.echo(f"  indice: {ready.path}")
            else:
                typer.echo(f"  el indice ya existe: {ready.path}")
            raise typer.Exit(code=0)

    if output is None:
        raise typer.BadParameter("falta --output")
    if Path(output).exists():
        typer.echo(f"  el indice ya existe: {output} (borralo para reconstruirlo)")
        raise typer.Exit(code=0)

    typer.echo(f"  leyendo {pbf} ...")
    stats = build(pbf, output, location_index=location_index)
    size_mb = Path(output).stat().st_size / (1024 * 1024)
    typer.echo(f"  {stats.nodes + stats.ways} lugares indexados "
               f"({stats.with_address} con direccion, {stats.named} con nombre)")
    typer.echo(f"  indice: {output} ({size_mb:.1f} MB)")


@app.command("list-pbf")
def list_pbf(
    pbf_dir: Path = typer.Option(None, "--pbf-dir"),
    lat: float = typer.Option(None, help="Mostrar solo los que cubren este punto."),
    lon: float = typer.Option(None),
) -> None:
    """Lista los .osm.pbf disponibles y cual cubre un punto dado."""
    from .geocoding.extract import scan_registry

    cfg = Config.from_env()
    root = pbf_dir or cfg.pbf_dir
    if not root:
        raise typer.BadParameter("indica --pbf-dir o SMART_IMPORT_PBF_DIR")
    registry = scan_registry(root, cfg.extract_dir)
    typer.echo(f"  {len(registry.entries)} PBF en {root} "
               f"({len(registry.with_bbox())} con bbox en el nombre)", err=True)
    if lat is not None and lon is not None:
        entry = registry.resolve(lat=lat, lon=lon)
        _echo_json({"point": [lat, lon],
                    "selected": entry.as_dict() if entry else None,
                    "covering": [e.as_dict() for e in registry.with_bbox()
                                 if e.covers(lat, lon)][:10]})
    else:
        for e in registry.entries[:40]:
            bbox = (f"n{e.north} s{e.south} e{e.east} w{e.west}" if e.has_bbox else "sin bbox")
            typer.echo(f"    {e.zone:20} {e.path.name:44} {e.size_bytes / 1e6:8.1f} MB  {bbox}")


@app.command("build-street-aliases")
def build_street_aliases(
    pbf_dir: Path = typer.Option(None, "--pbf-dir", help="Raiz de PBFs (o SMART_IMPORT_PBF_DIR)."),
    output: Path = typer.Option(None, "--output", "-o", help="SQLite de alias."),
    only: str = typer.Option(
        "", "--only",
        help="Filtrar por substring del path, separado por comas (belgium,luxembourg)."),
    include_extracts: bool = typer.Option(
        False, "--include-extracts",
        help="Incluir recortes n.._s.. (por defecto solo pais/region)."),
    force: bool = typer.Option(False, "--force", help="Re-barrer aunque el PBF no haya cambiado."),
    max_files: int = typer.Option(None, "--max-files", help="Tope de archivos (pruebas)."),
    replace: bool = typer.Option(
        False, "--replace", help="Borra el sqlite y barre de cero (si no, INSERT OR IGNORE)."),
) -> None:
    """Barre los PBF locales y arma el mapa name ↔ name:fr/nl/de.

    No abre geometria: solo tags. El geocoder usa el sqlite como expansion de
    query, asi un indice viejo que solo guardo `name` encuentra Avenue Mozart
    cuando OSM tiene Mozartstraat.
    """
    from .geocoding.name_aliases import StreetAliasStore, harvest_pbf_dir

    cfg = Config.from_env()
    root = pbf_dir or (Path(cfg.pbf_dir) if cfg.pbf_dir else None)
    if not root:
        raise typer.BadParameter("indica --pbf-dir o SMART_IMPORT_PBF_DIR")
    dest = output or Path(cfg.street_aliases_path)
    if replace:
        dest.unlink(missing_ok=True)
        Path(str(dest) + "-wal").unlink(missing_ok=True)
        Path(str(dest) + "-shm").unlink(missing_ok=True)
    only_tuple = tuple(p.strip() for p in only.split(",") if p.strip())
    store = StreetAliasStore(dest, readonly=False)
    try:
        typer.echo(f"  barrido {root} → {dest}", err=True)

        def progress(kind: str, path: Path, added: int) -> None:
            mark = {"ok": "+", "skip": ".", "error": "!"}[kind]
            extra = f" +{added} pares" if kind == "ok" else ""
            typer.echo(f"  {mark} {path.name}{extra}", err=True)

        stats = harvest_pbf_dir(
            root, store,
            include_extracts=include_extracts,
            only=only_tuple,
            force=force,
            max_files=max_files,
            progress=progress,
        )
    finally:
        store.close()
    typer.echo(
        f"  listo: {stats.files} PBF nuevos, {stats.skipped} ya barridos, "
        f"{stats.pairs} pares insertados, {len(stats.errors)} errores"
    )
    if stats.errors:
        for err in stats.errors[:8]:
            typer.echo(f"    {err}", err=True)
        raise typer.Exit(code=1)


@app.command()
def geocode(
    input: Path = typer.Option(..., "--input", "-i", exists=True, help="CSV ya normalizado."),
    output: Path = typer.Option(..., "--output", "-o"),
    index: Path = typer.Option(None, "--index", help="Indice .sqlite a usar."),
    origin_lat: float = typer.Option(None, "--origin-lat", help="Depot: sesga y desempata."),
    origin_lon: float = typer.Option(None, "--origin-lon"),
    depot_city: str = typer.Option(
        None, "--depot-city",
        help="Ciudad del depot (enrichment). Varias palabras: San Francisco."),
    depot_region: str = typer.Option(None, "--depot-region"),
    depot_postcode: str = typer.Option(None, "--depot-postcode"),
    depot_country: str = typer.Option(
        None, "--depot-country",
        help="Pais del depot. Varias palabras: United States."),
    depot_address: str = typer.Option(
        None, "--depot-address",
        help="Direccion libre del depot (NO se parsea para enrichment)."),
    max_distance_km: float = typer.Option(
        None, "--max-distance-km",
        help="Geofence duro en km (default GEOCODE_MAX_DISTANCE_KM)."),
    bbox: str = typer.Option(None, help="north,south,east,west para acotar la busqueda."),
    pbf_dir: Path = typer.Option(None, "--pbf-dir"),
    index_dir: Path = typer.Option(None, "--index-dir"),
    build_missing: bool = typer.Option(True, help="Construir el indice si no existe."),
    enhance_addresses: bool = typer.Option(
        False, "--enhance-addresses",
        help="Reescribe la query con parser/enhance; no muta el address del CSV."),
) -> None:
    """Completa coordenadas de las filas que tienen direccion y no tienen lat/lng."""
    from .geocoding.depot_context import depot_from_params
    from .geocoding.extract import ExtractError, ensure_geocode_index_from_config
    from .geocoding.runner import run

    cfg = Config.from_env()
    origin = (origin_lat, origin_lon) if origin_lat is not None and origin_lon is not None else None
    box = tuple(float(v) for v in bbox.split(",")) if bbox else None
    if box and len(box) != 4:
        raise typer.BadParameter("--bbox espera north,south,east,west")

    depot = depot_from_params(
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        depot_city=depot_city,
        depot_region=depot_region,
        depot_postcode=depot_postcode,
        depot_country=depot_country,
        depot_address=depot_address,
        max_distance_km=(
            float(max_distance_km)
            if max_distance_km is not None
            else float(cfg.max_geocode_distance_km)
        ),
    )

    if index is None:
        root = pbf_dir or cfg.pbf_dir
        if not root:
            raise typer.BadParameter("indica --index, o --pbf-dir/SMART_IMPORT_PBF_DIR con --origin-lat/--origin-lon")
        try:
            ready = ensure_geocode_index_from_config(
                cfg.replace(autobuild_index=build_missing),
                lat=origin_lat, lon=origin_lon, bbox=box,
                pbf_dir=root, index_dir=index_dir or cfg.index_dir,
            )
        except (FileNotFoundError, ExtractError) as exc:
            typer.echo(f"  {exc}", err=True)
            raise typer.Exit(code=1)
        index = ready.path
        typer.echo(f"  indice: {index.name}"
                   f"{' (extract generado)' if ready.cut_extract else ''}")

    def progress(done, rep):
        typer.echo(f"    {done} filas... ({rep.matched} matched, {rep.low_confidence} low, "
                   f"{rep.not_found} not found, {rep.rejected_far} far)")

    report = run(input, output, index, origin=origin, bbox=box, config=cfg,
                 progress=progress, depot=depot,
                 enhance_addresses=bool(enhance_addresses))
    d = report.as_dict()
    typer.echo(f"  {d['rows']} filas | {d['already_geocoded']} ya tenian coordenadas")
    typer.echo(f"  {d['matched']} geocodificadas | {d['low_confidence']} confianza baja | "
               f"{d['not_found']} no encontradas | {d['errors']} errores")
    if d.get("rejected_far") or d.get("enriched"):
        typer.echo(f"  enrichment: {d.get('enriched', 0)} | fuera de radio: {d.get('rejected_far', 0)}")
    typer.echo(f"  cache: {d['cache']['hits']} hits / {d['cache']['misses']} misses "
               f"({d['cache']['hit_rate']:.0%})")
    typer.echo(f"  {d['elapsed_s']}s ({d['rows_per_s']} filas/s)")
    typer.echo(f"  salida: {output}")

    report_path = Path(str(output).rsplit(".", 1)[0] + ".geocode.report.json")
    report_path.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    typer.echo(f"  report: {report_path}")


def _accuracy_run_slice(
    *,
    truth_path: Path,
    filas: list[dict],
    columnas: dict[str, str],
    cfg: Config,
    index: Path | None,
    pbf_dir: Path | None,
    index_dir: Path | None,
    origin_lat: float | None,
    origin_lon: float | None,
    depot_city: str | None,
    depot_region: str | None,
    depot_postcode: str | None,
    depot_country: str | None,
    depot_address: str | None,
    fence_km: float,
    enhance: bool,
    zone_hint: str | None = None,
):
    """Elige índice por bbox del slice y corre accuracy. None = sin cobertura."""
    from .geocoding.accuracy import data_bbox, resolve_accuracy_origin, run
    from .geocoding.coord_check import audit_truth_coords
    from .geocoding.depot_context import depot_from_params
    from .geocoding.extract import ExtractError, ensure_geocode_index_from_config

    bbox = data_bbox(filas, columnas)
    if bbox is None:
        return None, "sin coordenadas validas"
    audit = audit_truth_coords(
        filas, columnas, " ".join(
            p for p in (depot_country, zone_hint) if p
        ) or None,
    )
    if audit.skip_reason:
        return None, audit.skip_reason
    try:
        chosen = resolve_accuracy_origin(
            filas, columnas,
            origin_lat=origin_lat, origin_lon=origin_lon,
            max_distance_km=fence_km,
        )
    except ValueError as exc:
        return None, str(exc)

    ns = bbox[0] - bbox[1]
    ew = abs(bbox[2] - bbox[3])
    if ns > 1.5 or ew > 1.5:
        typer.echo(
            "  aviso: el corpus cubre más de ~150 km "
            f"(N{bbox[0]:.2f} S{bbox[1]:.2f} E{bbox[2]:.2f} W{bbox[3]:.2f}); "
            "el extract se recorta a ~80 km del depot. "
            "Filas de otra región no están en el índice — no es el scorer.",
            err=True,
        )

    local_index = index
    extra = ""
    if local_index is None:
        raiz = pbf_dir or cfg.pbf_dir
        if not raiz:
            raise typer.BadParameter("indica --index, o --pbf-dir/SMART_IMPORT_PBF_DIR")
        try:
            ready = ensure_geocode_index_from_config(
                cfg, lat=chosen.lat, lon=chosen.lon, bbox=bbox,
                zone_hint=zone_hint,
                pbf_dir=raiz, index_dir=index_dir or cfg.index_dir,
            )
        except (FileNotFoundError, ExtractError) as exc:
            return None, str(exc)
        local_index = ready.path
        extra = " (extract generado)" if ready.cut_extract else ""

    depot = depot_from_params(
        origin_lat=chosen.lat, origin_lon=chosen.lon,
        depot_city=depot_city, depot_region=depot_region,
        depot_postcode=depot_postcode, depot_country=depot_country,
        depot_address=depot_address,
        max_distance_km=fence_km,
    )
    reporte = run(
        truth_path, local_index, origin=(chosen.lat, chosen.lon),
        config=cfg, limit=None, depot=depot, enhance=enhance,
        filas=filas, columnas=columnas,
    )
    return {
        "report": reporte,
        "index": local_index,
        "chosen": chosen,
        "bbox": bbox,
        "extra": extra,
    }, None


def _emit_accuracy_report(
    reporte,
    *,
    dump: bool,
    dump_only: str,
    dump_out: Path | None,
    out: Path | None,
    split_by_housenumber: bool,
    fail_under: float | None,
) -> None:
    from .geocoding.accuracy import (
        format_report, format_rows_table, table_data_row_count,
    )

    if reporte.depot_warning:
        typer.echo(f"  aviso: {reporte.depot_warning}", err=True)
    if reporte.depot_tokens:
        typer.echo(f"  depot enrich (como la UI): {', '.join(reporte.depot_tokens)}")
    typer.echo("")
    if dump:
        tabla = format_rows_table(reporte.rows, only=dump_only)
        n_filas = table_data_row_count(tabla)
        typer.echo(
            f"tabla ({n_filas} filas, filtro={dump_only}, "
            f"orden=cerca→lejos, sin pin al final):")
        typer.echo(tabla)
        typer.echo("")
    if reporte.regions:
        typer.echo("totales del mix (países medidos juntos; no es un solo país):")
    typer.echo(format_report(reporte, split=split_by_housenumber))
    if dump_out:
        dump_out.write_text(
            json.dumps(reporte.rows, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        typer.echo(f"\n  filas: {dump_out}")

    d = reporte.as_dict()
    typer.echo("\n  peores 5:")
    for w in d["worst"][:5]:
        where = w.get("country") or w.get("preset") or ""
        tag = f"{where}  " if where else ""
        typer.echo(f"    {w['error_m']:8.0f} m  [{w['band']}] {tag}{w['address'][:44]}")

    if out:
        Path(out).write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        typer.echo(f"\n  reporte: {out}")

    if fail_under is not None:
        clave = d["with_house_number"] if split_by_housenumber else d["overall"]
        logrado = clave["within_100m_pct"]
        typer.echo(f"\n  gate: {logrado}% dentro de 100 m (minimo {fail_under}%)")
        if logrado < fail_under:
            raise typer.Exit(1)


@app.command("geocode-accuracy")
def geocode_accuracy(
    truth: Path = typer.Option(..., "--truth",
                               help="CSV/JSON/XLSX con direcciones Y coordenadas verificadas. "
                               "Si no existe y el nombre es {preset}[_fixture]_n{N}.json, "
                               "se genera automáticamente."),
    generate_truth: bool = typer.Option(
        True, "--generate-truth/--no-generate-truth",
        help="Si --truth no existe, generarlo (fixture local o OpenAddresses Batch)."),
    index: Path = typer.Option(None, "--index", help="Indice .sqlite. Por defecto se elige por bbox."),
    origin_lat: float = typer.Option(
        None, "--origin-lat",
        help="Depot (desempate). Opcional: default = 1er address del truth. "
             "Si queda lejos del corpus, se ignora."),
    origin_lon: float = typer.Option(None, "--origin-lon"),
    depot_city: str = typer.Option(
        None, "--depot-city",
        help="Geolocalizador (Near CABA). Si queda lejos del corpus, no se inyecta."),
    depot_region: str = typer.Option(None, "--depot-region"),
    depot_postcode: str = typer.Option(None, "--depot-postcode"),
    depot_country: str = typer.Option(
        None, "--depot-country",
        help="Pais del depot. Varias palabras: United States."),
    depot_address: str = typer.Option(
        None, "--depot-address",
        help="Dirección libre del depot (NO se parsea para enrichment)."),
    max_distance_km: float = typer.Option(
        None, "--max-distance-km",
        help="Geofence del form (default GEOCODE_MAX_DISTANCE_KM)."),
    enhance: bool = typer.Option(
        False, "--enhance/--no-enhance",
        help="Como enhance_addresses de la UI: reescribe road+altura en la query."),
    libpostal: bool = typer.Option(
        True, "--libpostal/--no-libpostal",
        help="Usar libpostal on-demand si esta instalado (default: si)."),
    pbf_dir: Path = typer.Option(None, "--pbf-dir"),
    index_dir: Path = typer.Option(None, "--index-dir"),
    split_by_housenumber: bool = typer.Option(
        True, "--split-by-housenumber/--no-split",
        help="Separar direcciones con altura de las que no la tienen."),
    by_country: bool | None = typer.Option(
        None, "--by-country/--no-by-country",
        help="Un índice+depot por país (mix mundial). Default: auto si hay varios country/preset."),
    limit: int = typer.Option(None, help="Evaluar solo las primeras N."),
    fail_under: float = typer.Option(
        None, "--fail-under",
        help="Salir con codigo 1 si el %% dentro de 100 m (con altura) queda debajo."),
    out: Path = typer.Option(None, "--out", help="Guardar el reporte JSON (resumen)."),
    dump: bool = typer.Option(
        True, "--dump/--no-dump",
        help="Tabla fila a fila: coords, address in/sent, metros, confianza."),
    dump_only: str = typer.Option(
        "all", "--dump-only",
        help="all | house | pin | miss | far (>250m) | over (valid y lejos)."),
    dump_out: Path = typer.Option(
        None, "--dump-out",
        help="JSON con todas las filas (address, sent, coords)."),
) -> None:
    """Cuantas direcciones acierta el geocoder, y a cuantos metros.

    Es la medicion que decide si esto se puede ofrecer como servicio. El indice se
    elige por el bbox de los DATOS: medir con el indice equivocado da kilometros de
    error y parece un problema de scoring cuando es de cobertura. Un mix de
    varios países parte por preset/country (un PBF no cubre el bbox mundial).
    """
    from .geocoding.accuracy import (
        RegionOutcome, compact_region_summary, data_bbox,
        format_regions_table, group_truth_rows, is_multi_region_truth,
        load_truth, merge_accuracy_reports,
    )
    from .tools.address_corpus import (
        OpenAddressesError, ensure_truth_corpus, preset_for_group,
    )

    truth_path = truth.expanduser()
    if not truth_path.is_file():
        if not generate_truth:
            raise typer.BadParameter(f"Path '{truth}' does not exist.")
        try:
            truth_path = ensure_truth_corpus(truth_path)
        except OpenAddressesError as exc:
            typer.echo(f"  {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"  truth generado: {truth_path}")
        if truth_path.with_suffix(".csv").is_file():
            typer.echo(f"  csv:            {truth_path.with_suffix('.csv')}")

    cfg = Config.from_env()
    from .addresses.libpostal_parser import is_installed
    if libpostal:
        if is_installed():
            cfg = cfg.replace(libpostal_enabled=True)
        else:
            typer.echo(
                "  libpostal pedido pero no esta instalado "
                "(brew install libpostal && pip install -e '.[libpostal]')",
                err=True,
            )
    else:
        cfg = cfg.replace(libpostal_enabled=False)
    from .addresses.factory import build_address_parser
    parser = build_address_parser(cfg)
    typer.echo(f"  parser={parser.name}  libpostal="
               f"{'on-demand' if cfg.libpostal_enabled and is_installed() else 'off'}")
    filas, columnas = load_truth(truth_path)
    typer.echo(f"  {len(filas)} filas | columnas detectadas: "
               f"address={columnas['address']!r} lat={columnas['lat']!r} lng={columnas['lng']!r}")

    work = filas[:limit] if limit else filas
    if limit:
        typer.echo(f"  limit={limit}: {len(work)} filas")

    fence_km = (
        float(max_distance_km)
        if max_distance_km is not None
        else float(cfg.max_geocode_distance_km)
    )
    if by_country is False:
        multi = False
    elif by_country is True:
        multi = True
    else:
        multi = is_multi_region_truth(work)

    if multi:
        if index is not None:
            typer.echo("  aviso: --index ignorado en mix (un índice por país)", err=True)
        if depot_city or depot_country or origin_lat is not None:
            typer.echo(
                "  aviso: --depot-city/--origin-* ignorados en mix "
                "(cada país usa el del preset)",
                err=True,
            )
        groups = group_truth_rows(work)
        real_keys = [k for k, _ in groups if k != "_unknown"]
        typer.echo(
            f"  mix de {len(real_keys)} regiones: un índice + depot por país "
            "(un PBF no cubre el bbox mundial)")
        if limit:
            typer.echo(
                f"  aviso: --limit {limit} sobre {len(real_keys)} países ≈ "
                f"{len(work) // max(len(real_keys), 1)} filas c/u "
                "(humo, no medición). Quitá --limit para el corpus entero.",
                err=True,
            )
        reports = []
        outcomes: list[RegionOutcome] = []
        for key, group_rows in groups:
            if key == "_unknown":
                typer.echo(f"  {key:<22} SKIP  {len(group_rows)} filas sin country/preset")
                outcomes.append(RegionOutcome(
                    key=key, n_input=len(group_rows), skip="sin country/preset"))
                continue
            preset = preset_for_group(key)
            zone_parts = [key]
            if preset:
                if preset.country:
                    zone_parts.append(preset.country)
                if preset.depot_country:
                    zone_parts.append(preset.depot_country)
                if preset.depot_city:
                    zone_parts.append(preset.depot_city)
            zone = " ".join(zone_parts)
            packed, reason = _accuracy_run_slice(
                truth_path=truth_path, filas=group_rows, columnas=columnas,
                cfg=cfg, index=None, pbf_dir=pbf_dir, index_dir=index_dir,
                origin_lat=preset.depot_lat if preset else None,
                origin_lon=preset.depot_lon if preset else None,
                depot_city=preset.depot_city if preset else None,
                depot_region=None, depot_postcode=None,
                depot_country=preset.depot_country if preset else None,
                depot_address=None, fence_km=fence_km, enhance=enhance,
                zone_hint=zone,
            )
            if packed is None:
                typer.echo(f"  {key:<22} SKIP  {reason}")
                outcomes.append(RegionOutcome(
                    key=key, n_input=len(group_rows), skip=str(reason)))
                continue
            reporte = packed["report"]
            reports.append(reporte)
            outcomes.append(RegionOutcome(
                key=key, n_input=len(group_rows), report=reporte,
                index_name=packed["index"].name + packed["extra"],
            ))
            typer.echo(
                compact_region_summary(key, reporte)
                + f"  [{packed['index'].name}{packed['extra']}]")
        if not reports:
            typer.echo("  ningún país tuvo cobertura PBF", err=True)
            raise typer.Exit(1)
        typer.echo("")
        typer.echo(format_regions_table(outcomes))
        merged = merge_accuracy_reports(str(truth_path), reports)
        merged.regions = [o.as_dict() for o in outcomes]
        _emit_accuracy_report(
            merged, dump=dump, dump_only=dump_only, dump_out=dump_out,
            out=out, split_by_housenumber=split_by_housenumber,
            fail_under=fail_under,
        )
        return

    bbox = data_bbox(work, columnas)
    if bbox is None:
        typer.echo("  el archivo no tiene ninguna coordenada valida", err=True)
        raise typer.Exit(1)
    typer.echo(f"  bbox de los datos: N{bbox[0]:.3f} S{bbox[1]:.3f} E{bbox[2]:.3f} W{bbox[3]:.3f}")

    packed, reason = _accuracy_run_slice(
        truth_path=truth_path, filas=work, columnas=columnas, cfg=cfg,
        index=index, pbf_dir=pbf_dir, index_dir=index_dir,
        origin_lat=origin_lat, origin_lon=origin_lon,
        depot_city=depot_city, depot_region=depot_region,
        depot_postcode=depot_postcode, depot_country=depot_country,
        depot_address=depot_address, fence_km=fence_km, enhance=enhance,
        zone_hint=" ".join(
            p for p in (depot_country, depot_city, depot_region) if p) or None,
    )
    if packed is None:
        typer.echo(f"  {reason}", err=True)
        raise typer.Exit(1)
    chosen = packed["chosen"]
    if chosen.warning:
        typer.echo(f"  aviso: {chosen.warning}", err=True)
    if chosen.source == "first-address":
        clip = (chosen.address[:44] + "…") if len(chosen.address) > 44 else chosen.address
        extra = f"  [1er address: {clip}]" if clip else "  [1er address del truth]"
    else:
        extra = "  [--origin-lat/lon]"
    typer.echo(f"  indice elegido por bbox: {packed['index'].name}{packed['extra']}")
    typer.echo(f"  origen (desempate): {chosen.lat:.4f},{chosen.lon:.4f}{extra}")

    _emit_accuracy_report(
        packed["report"], dump=dump, dump_only=dump_only, dump_out=dump_out,
        out=out, split_by_housenumber=split_by_housenumber,
        fail_under=fail_under,
    )


@app.command("geocode-eval")
def geocode_eval(
    truth: Path = typer.Option(..., "--truth", exists=True,
                               help="CSV normalizado que YA tiene lat/lng correctas."),
    index: Path = typer.Option(..., "--index", exists=True),
    origin_lat: float = typer.Option(None, "--origin-lat"),
    origin_lon: float = typer.Option(None, "--origin-lon"),
    limit: int = typer.Option(None, help="Evaluar solo las primeras N filas."),
) -> None:
    """Mide cobertura y error en metros contra direcciones de coordenadas conocidas."""
    from .geocoding.evaluate import run

    origin = (origin_lat, origin_lon) if origin_lat is not None and origin_lon is not None else None
    ev = run(truth, index, origin=origin, config=Config.from_env(), limit=limit)
    d = ev.as_dict()
    typer.echo(f"  {d['total']} direcciones evaluadas contra {index.name}")
    typer.echo(f"  cobertura: {d['coverage_pct']}%  (exactas {d['exact_pct']}%, "
               f"baja confianza {d['low_confidence']}, sin resultado {d['not_found']})")
    e = d["error_m"]
    if e["median"] is not None:
        typer.echo(f"  error: mediana {e['median']} m | p90 {e['p90']} m | max {e['max']} m")
        typer.echo(f"  dentro de 100 m: {e['under_100m_pct']}%  |  dentro de 500 m: {e['under_500m_pct']}%")
    _echo_json({"worst": d["worst"]})


@app.command("geocode-regression")
def geocode_regression(
    manifest: Path = typer.Option(
        None, "--manifest", help="Suites a correr (default: examples/geocode-truth/regression/manifest.json)."),
    profile: str = typer.Option("fast", "--profile", help="fast | full (limites por suite)."),
    label: str = typer.Option(..., "--label", help="Nombre del snapshot (baseline, fix-gate, ...)."),
    out: Path = typer.Option(Path("out/regression"), "--out", help="Carpeta raiz de snapshots."),
    only: str = typer.Option(None, "--only", help="Ids de suite separados por coma."),
    tags: str = typer.Option(None, "--tags", help="Solo suites con alguno de estos tags (coma)."),
    against: Path = typer.Option(
        None, "--against", help="Snapshot previo: al terminar imprime el diff y sale 1 si hay regresiones."),
    env_file: Path = typer.Option(
        None, "--env-file", exists=True,
        help="Umbrales de un despliegue (GEOCODE_*): p.ej. deploy/templates/api-prod.env.template."),
) -> None:
    """Geocodifica cada suite por el MISMO camino que produccion y guarda un snapshot.

    A diferencia de `geocode-accuracy`, pasa por `geocoding.runner.run`: gate de
    evidencia, bandas con force_review, geofences y reintento limpio incluidos.
    Cada fila se califica contra su verdad y el diff entre snapshots lista las
    regresiones una por una.
    """
    from .tools.geocode_regression import (
        DEFAULT_MANIFEST, diff_snapshots, format_diff, format_summary,
        load_manifest, read_snapshot, run_suite, write_snapshot, write_suite_rows,
    )

    if env_file is not None:
        import os
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.startswith("GEOCODE_") and not key.startswith("#"):
                os.environ[key] = value.strip()
    suites = load_manifest(manifest or DEFAULT_MANIFEST)
    wanted = {s.strip() for s in (only or "").split(",") if s.strip()}
    wanted_tags = {s.strip() for s in (tags or "").split(",") if s.strip()}
    selected = [s for s in suites
                if profile in s.profiles
                and (not wanted or s.id in wanted)
                and (not wanted_tags or wanted_tags & set(s.tags))]
    if not selected:
        raise typer.BadParameter("ninguna suite coincide con el filtro")

    target = out / label
    runs = []
    for suite in selected:
        typer.echo(f"  ▸ {suite.id} ({suite.corpus.name})", err=True)
        runs.append(run_suite(suite, profile=profile, workdir=target / "_work"))
        write_suite_rows(runs[-1], target)
    write_snapshot(runs, target, label=label, profile=profile)
    summary = json.loads((target / "summary.json").read_text(encoding="utf-8"))
    typer.echo(format_summary(summary))
    typer.echo(f"\n  snapshot: {target}")

    if against is not None:
        result = diff_snapshots(read_snapshot(against), read_snapshot(target))
        typer.echo("\n" + format_diff(result))
        if any(c.kind == "regression" for c in result["changes"]):
            raise typer.Exit(1)


@app.command("geocode-regression-diff")
def geocode_regression_diff(
    before: Path = typer.Argument(..., exists=True, help="Snapshot base."),
    after: Path = typer.Argument(..., exists=True, help="Snapshot candidato."),
    max_improvements: int = typer.Option(40, "--max-improvements"),
    json_out: Path = typer.Option(None, "--json", help="Guardar el diff completo en JSON."),
) -> None:
    """Regresiones / mejoras / deriva fila por fila entre dos snapshots. Sale 1 si hay regresiones."""
    from .tools.geocode_regression import diff_snapshots, format_diff, read_snapshot

    result = diff_snapshots(read_snapshot(before), read_snapshot(after))
    typer.echo(format_diff(result, max_improvements=max_improvements))
    if json_out is not None:
        payload = {
            "changes": [{"kind": c.kind, "key": c.key, "suite": c.suite,
                         "address": c.address, "before": c.before, "after": c.after}
                        for c in result["changes"]],
            "per_suite": result["per_suite"], "per_noise": result["per_noise"],
        }
        json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(c.kind == "regression" for c in result["changes"]):
        raise typer.Exit(1)


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="0.0.0.0 para exponerlo al host."),
    port: int = typer.Option(8100, help="Puerto HTTP."),
    reload: bool = typer.Option(False, help="Auto-reload (desarrollo)."),
    workers: int = typer.Option(
        1, help="Procesos uvicorn. Mas de 1 requiere SMART_IMPORT_ROLE=api "
                "(estado compartido); en embedded solo vale 1."),
) -> None:
    """Levanta la API HTTP.

    Cuantos procesos se pueden levantar depende de DONDE vive el estado, y eso
    lo decide `SMART_IMPORT_ROLE`:

      · `embedded` (default): los jobs viven en la memoria de ESTE proceso, asi
        que `--workers` tiene que ser 1. Para dar mas capacidad se agregan
        INSTANCIAS con balanceo sticky, de modo que cada job vuelva siempre a la
        suya; con round-robin el POST /imports cae en una y el POST /geocode en
        otra, que responde 404.

      · `api`: el estado esta en Redis y los archivos se sirven por rutas
        compartidas, asi que cualquier proceso puede atender cualquier job.
        Varios workers de uvicorn son validos, y el balanceo NO necesita ser
        sticky.
    """
    import uvicorn

    cfg = Config.from_env()
    # El guard va ANTES del banner. Al revés, el log dice "escuchando en :8100"
    # y despues falla: alguien mirando `docker logs` lee que el servicio arranco
    # cuando en realidad salio con exit 2 y no hay nadie atendiendo el puerto.
    if workers > 1 and cfg.role == "embedded":
        # Era un aviso, y un aviso no impide nada: quien levantaba el servicio
        # con --workers 4 para "escalar" rompia el flujo de forma intermitente.
        # El POST /imports cae en un worker y el POST /geocode en otro, que
        # responde 404 porque no conoce ese job.
        #
        # El guard es sobre `embedded`, no sobre `--workers`: en rol `api` el
        # estado esta en Redis y varios procesos son exactamente lo que se
        # quiere. Dejarlo como un no-negociable seria pedirle al operador que
        # levante N containers para algo que uvicorn ya sabe hacer.
        typer.echo(
            f"  ERROR: --workers {workers} no es valido con "
            f"SMART_IMPORT_ROLE=embedded.\n"
            "  El almacen de jobs vive en memoria del proceso: con mas de un\n"
            "  worker, el import y su geocode caen en procesos distintos y el\n"
            "  segundo responde 404. Usa --workers 1.\n"
            "  Para varios procesos, pone SMART_IMPORT_ROLE=api con Redis: ahi\n"
            "  el estado es compartido y el balanceo no necesita ser sticky.",
            err=True)
        raise typer.Exit(2)
    typer.echo(f"  Smart Import escuchando en http://{host}:{port}")
    typer.echo(f"  docs: http://localhost:{port}/docs")
    typer.echo(f"  parser: {cfg.address_parser} | libpostal: {'on' if cfg.libpostal_enabled else 'off'}"
               f" | PBF dir: {cfg.pbf_dir or '(sin configurar)'}")
    # limit_concurrency: la puerta mas externa. Por encima de este numero de
    # tareas, uvicorn corta antes de que el request toque la app, y asi una
    # avalancha no se traduce en miles de conexiones abiertas comiendo memoria.
    # Ojo con bajarlo: los SSE de progreso son conexiones abiertas y cuentan.
    limite = cfg.http_limit_concurrency
    if limite > 0:
        typer.echo(f"  techo de concurrencia HTTP: {limite} tareas | "
                   f"imports en paralelo: {cfg.max_concurrent_normalize} "
                   f"(cola de admision {cfg.max_normalize_queue})")
    uvicorn.run("smart_import.api:app", host=host, port=port, reload=reload,
                workers=workers, limit_concurrency=limite or None)


@app.command()
def worker(
    check: bool = typer.Option(
        False, "--check",
        help="Prueba cola, estado y archivos, y sale. Para dar de alta un nodo.",
    ),
) -> None:
    """Consume tareas de la cola. No sirve HTTP.

    Es el otro lado de `SMART_IMPORT_ROLE=api`: la API recibe los archivos y
    encola, este proceso hace el trabajo pesado y devuelve los resultados. Toda
    la configuracion sale del entorno; si falta algo, no arranca y dice que.

    Con `--check` no consume nada: toca las tres dependencias y reporta cual
    falla. Es lo primero que conviene correr en una maquina nueva, antes de
    dejar el servicio prendido.
    """
    from .worker.main import main

    raise typer.Exit(main(solo_verificar=check))


if __name__ == "__main__":
    main()




