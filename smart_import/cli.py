"""CLI de Vepathos Smart Import."""
from __future__ import annotations

import json
from pathlib import Path

import typer

from .config import Config
from .mapping import build_mapper
from .readers import read_any
from .schemas import TargetSchema

app = typer.Typer(add_completion=False, help="Vepathos Smart Import - normaliza archivos de entregas.")

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
    app()


if __name__ == "__main__":
    main()


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


@app.command()
def geocode(
    input: Path = typer.Option(..., "--input", "-i", exists=True, help="CSV ya normalizado."),
    output: Path = typer.Option(..., "--output", "-o"),
    index: Path = typer.Option(None, "--index", help="Indice .sqlite a usar."),
    origin_lat: float = typer.Option(None, "--origin-lat", help="Depot: sesga y desempata."),
    origin_lon: float = typer.Option(None, "--origin-lon"),
    depot_city: str = typer.Option(None, "--depot-city", help="Ciudad del depot (enrichment)."),
    depot_region: str = typer.Option(None, "--depot-region"),
    depot_postcode: str = typer.Option(None, "--depot-postcode"),
    depot_country: str = typer.Option(None, "--depot-country"),
    depot_address: str = typer.Option(None, "--depot-address",
                                      help="Direccion libre del depot."),
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


@app.command("geocode-accuracy")
def geocode_accuracy(
    truth: Path = typer.Option(..., "--truth", exists=True,
                               help="CSV/JSON/XLSX con direcciones Y coordenadas verificadas."),
    index: Path = typer.Option(None, "--index", help="Indice .sqlite. Por defecto se elige por bbox."),
    origin_lat: float = typer.Option(None, "--origin-lat", help="Depot: sesga y desempata."),
    origin_lon: float = typer.Option(None, "--origin-lon"),
    depot_city: str = typer.Option(
        None, "--depot-city",
        help="Geolocalizador de la UI (Near CABA). Misma query que POST /geocode."),
    depot_region: str = typer.Option(None, "--depot-region"),
    depot_postcode: str = typer.Option(None, "--depot-postcode"),
    depot_country: str = typer.Option(None, "--depot-country"),
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
    error y parece un problema de scoring cuando es de cobertura.
    """
    from .geocoding.accuracy import (
        data_bbox, format_report, format_rows_table, load_truth, run,
    )
    from .geocoding.extract import ExtractError, ensure_geocode_index_from_config

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
    filas, columnas = load_truth(truth)
    typer.echo(f"  {len(filas)} filas | columnas detectadas: "
               f"address={columnas['address']!r} lat={columnas['lat']!r} lng={columnas['lng']!r}")

    bbox = data_bbox(filas, columnas)
    if bbox is None:
        typer.echo("  el archivo no tiene ninguna coordenada valida", err=True)
        raise typer.Exit(1)
    centro = ((bbox[0] + bbox[1]) / 2, (bbox[2] + bbox[3]) / 2)
    typer.echo(f"  bbox de los datos: N{bbox[0]:.3f} S{bbox[1]:.3f} E{bbox[2]:.3f} W{bbox[3]:.3f}")

    if index is None:
        raiz = pbf_dir or cfg.pbf_dir
        if not raiz:
            raise typer.BadParameter("indica --index, o --pbf-dir/SMART_IMPORT_PBF_DIR")
        try:
            ready = ensure_geocode_index_from_config(
                cfg, lat=centro[0], lon=centro[1], bbox=bbox,
                pbf_dir=raiz, index_dir=index_dir or cfg.index_dir,
            )
        except (FileNotFoundError, ExtractError) as exc:
            typer.echo(f"  {exc}", err=True)
            raise typer.Exit(1)
        index = ready.path
        extra = " (extract generado)" if ready.cut_extract else ""
        typer.echo(f"  indice elegido por bbox: {index.name}{extra}")

    origen = ((origin_lat, origin_lon)
              if origin_lat is not None and origin_lon is not None else centro)
    from .geocoding.depot_context import depot_from_params
    depot = depot_from_params(
        origin_lat=origen[0], origin_lon=origen[1],
        depot_city=depot_city, depot_region=depot_region,
        depot_postcode=depot_postcode, depot_country=depot_country,
        depot_address=depot_address,
        max_distance_km=(
            float(max_distance_km)
            if max_distance_km is not None
            else float(cfg.max_geocode_distance_km)
        ),
    )
    typer.echo(f"  origen (desempate): {origen[0]:.4f},{origen[1]:.4f}")

    reporte = run(truth, index, origin=origen, config=cfg, limit=limit,
                  depot=depot, enhance=enhance)
    if reporte.depot_tokens:
        typer.echo(f"  depot enrich (como la UI): {', '.join(reporte.depot_tokens)}")
    typer.echo("")
    if dump:
        tabla = format_rows_table(reporte.rows, only=dump_only)
        n_filas = max(0, len(tabla.splitlines()) - 2)
        typer.echo(f"tabla ({n_filas} filas, filtro={dump_only}):")
        typer.echo(tabla)
        typer.echo("")
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
        typer.echo(f"    {w['error_m']:8.0f} m  [{w['band']}] {w['address'][:44]}")

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


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="0.0.0.0 para exponerlo al host."),
    port: int = typer.Option(8100, help="Puerto HTTP."),
    reload: bool = typer.Option(False, help="Auto-reload (desarrollo)."),
    workers: int = typer.Option(1, help="Procesos uvicorn. Con 1 el store en memoria es consistente."),
) -> None:
    """Levanta la API HTTP.

    OJO: el almacen de jobs vive en memoria del proceso. Con workers > 1 cada
    worker veria jobs distintos. Para escalar hace falta el store compartido
    (Redis/Postgres), que es la etapa siguiente.
    """
    import uvicorn

    cfg = Config.from_env()
    typer.echo(f"  Smart Import escuchando en http://{host}:{port}")
    typer.echo(f"  docs: http://localhost:{port}/docs")
    typer.echo(f"  parser: {cfg.address_parser} | libpostal: {'on' if cfg.libpostal_enabled else 'off'}"
               f" | PBF dir: {cfg.pbf_dir or '(sin configurar)'}")
    if workers > 1:
        # Era un aviso, y un aviso no impide nada: quien levantaba el servicio
        # con --workers 4 para "escalar" rompia el flujo de forma intermitente.
        # El POST /imports cae en un worker y el POST /geocode en otro, que
        # responde 404 porque no conoce ese job. Falla el arranque hasta que el
        # almacen sea compartido (Redis/Postgres).
        typer.echo(
            f"  ERROR: --workers {workers} no es una configuracion valida.\n"
            "  El almacen de jobs vive en memoria del proceso: con mas de un\n"
            "  worker, el import y su geocode caen en procesos distintos y el\n"
            "  segundo responde 404. Usa --workers 1.\n"
            "  Para escalar hace falta el store compartido, no mas procesos.",
            err=True)
        raise typer.Exit(2)
    uvicorn.run("smart_import.api:app", host=host, port=port, reload=reload,
                workers=1)




@app.command()
def worker() -> None:
    """Consume smart-import-tasks de RabbitMQ (geocode async).

    Requiere RABBITMQ_HOST o RABBITMQ_URL. El archivo viaja por object storage
    (local o MinIO/S3); el mensaje solo lleva keys.
    """
    from .queue import SmartImportQueue
    from .storage import build_storage_from_env

    q = SmartImportQueue()
    if not q.enabled:
        typer.echo("  RabbitMQ no configurado. Setea RABBITMQ_URL o RABBITMQ_HOST.", err=True)
        raise typer.Exit(1)

    storage = build_storage_from_env()
    typer.echo(f"  worker escuchando {q.queue_name}")

    def handle(msg: dict) -> None:
        kind = msg.get("type")
        job_id = msg.get("job_id")
        typer.echo(f"  → {kind} job={job_id}")
        if kind == "smart_import.geocode":
            from .geocoding.runner import run
            from .config import Config
            from pathlib import Path

            opts = msg.get("options") or {}
            inp_key = msg["input_object_key"]
            out_key = msg["output_object_key"]
            local_in = storage.local_path(inp_key)
            if local_in is None or not local_in.exists():
                raw = storage.get(inp_key)
                local_in = Path(Config.from_env().work_dir) / job_id / "normalized.csv"
                local_in.parent.mkdir(parents=True, exist_ok=True)
                local_in.write_bytes(raw)
            local_out = Path(Config.from_env().work_dir) / job_id / "geocoded.csv"
            local_out.parent.mkdir(parents=True, exist_ok=True)
            index = opts.get("index")
            if not index:
                raise RuntimeError("worker geocode necesita options.index (ruta sqlite)")
            origin = None
            if opts.get("origin_lat") is not None and opts.get("origin_lon") is not None:
                origin = (float(opts["origin_lat"]), float(opts["origin_lon"]))
            run(str(local_in), str(local_out), Path(index), origin=origin,
                config=Config.from_env())
            storage.put(out_key, local_out.read_bytes(), "text/csv")
            typer.echo(f"  ✓ geocode {job_id} → {out_key}")
        else:
            typer.echo(f"  aviso: tipo desconocido {kind}", err=True)

    q.consume(handle)
