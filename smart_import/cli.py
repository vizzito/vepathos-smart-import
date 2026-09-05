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
    diagnostics: bool = typer.Option(False, help="Agrega columnas row_status/row_issues."),
    derive_volume: bool = typer.Option(False, help="Calcular volume_cm3 desde LxWxH si falta."),
    sheet: str = typer.Option(None),
) -> None:
    """Convierte el archivo al formato Vepathos. NUNCA geocodifica."""
    from .pipeline import run_normalize

    overrides = json.loads(Path(mapping).read_text(encoding="utf-8")) if mapping else None
    result = run_normalize(
        input, schema, output,
        emit=tuple(e.strip() for e in emit.split(",") if e.strip()),
        manual_mapping=overrides, phone_region=phone_region,
        diagnostics=diagnostics, sheet=sheet, derive_volume=derive_volume,
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
    typer.echo(f"  AI={'on' if cfg.ai_enabled else 'off'} device={cfg.device}\n")
    results = run(files, schema, repeats=repeats, out_dir=out, config=cfg)
    typer.echo(format_table(results))
    typer.echo(f"\n  resultados en {out}/results.csv y {out}/results.json")


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
    from .geocoding.osm_index import build, index_path_for
    from .geocoding.pbf_registry import PbfRegistry

    cfg = Config.from_env()
    target_index_dir = index_dir or Path(cfg.index_dir)

    if pbf is None:
        root = pbf_dir or cfg.pbf_dir
        if not root:
            raise typer.BadParameter("indica --pbf, o --pbf-dir/SMART_IMPORT_PBF_DIR con --origin-lat/--origin-lon")
        registry = PbfRegistry.scan(root)
        entry = registry.resolve(lat=origin_lat, lon=origin_lon)
        if entry is None:
            typer.echo(f"  sin cobertura PBF para ({origin_lat}, {origin_lon}) en {root}")
            typer.echo(f"  {len(registry.entries)} PBF disponibles")
            raise typer.Exit(code=1)
        pbf = entry.path
        output = output or index_path_for(entry, target_index_dir)
        typer.echo(f"  PBF elegido: {pbf.name} (zona {entry.zone}, {entry.size_bytes / 1e6:.1f} MB)")

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
    from .geocoding.pbf_registry import PbfRegistry

    cfg = Config.from_env()
    root = pbf_dir or cfg.pbf_dir
    if not root:
        raise typer.BadParameter("indica --pbf-dir o SMART_IMPORT_PBF_DIR")
    registry = PbfRegistry.scan(root)
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
    bbox: str = typer.Option(None, help="north,south,east,west para acotar la busqueda."),
    pbf_dir: Path = typer.Option(None, "--pbf-dir"),
    index_dir: Path = typer.Option(None, "--index-dir"),
    build_missing: bool = typer.Option(True, help="Construir el indice si no existe."),
) -> None:
    """Completa coordenadas de las filas que tienen direccion y no tienen lat/lng."""
    from .geocoding.osm_index import build, index_path_for
    from .geocoding.pbf_registry import PbfRegistry
    from .geocoding.runner import run

    cfg = Config.from_env()
    origin = (origin_lat, origin_lon) if origin_lat is not None and origin_lon is not None else None
    box = tuple(float(v) for v in bbox.split(",")) if bbox else None
    if box and len(box) != 4:
        raise typer.BadParameter("--bbox espera north,south,east,west")

    if index is None:
        root = pbf_dir or cfg.pbf_dir
        if not root:
            raise typer.BadParameter("indica --index, o --pbf-dir/SMART_IMPORT_PBF_DIR con --origin-lat/--origin-lon")
        registry = PbfRegistry.scan(root)
        entry = registry.resolve(lat=origin_lat, lon=origin_lon, bbox=box)
        if entry is None:
            typer.echo(f"  sin cobertura PBF para el area pedida en {root}")
            raise typer.Exit(code=1)
        index = index_path_for(entry, index_dir or cfg.index_dir)
        if not index.exists():
            if not build_missing:
                typer.echo(f"  falta el indice {index}")
                raise typer.Exit(code=1)
            typer.echo(f"  construyendo indice desde {entry.path.name} (una sola vez) ...")
            build(entry.path, index)

    def progress(done, rep):
        typer.echo(f"    {done} filas... ({rep.matched} matched, {rep.low_confidence} low, "
                   f"{rep.not_found} not found)")

    report = run(input, output, index, origin=origin, bbox=box, config=cfg, progress=progress)
    d = report.as_dict()
    typer.echo(f"  {d['rows']} filas | {d['already_geocoded']} ya tenian coordenadas")
    typer.echo(f"  {d['matched']} geocodificadas | {d['low_confidence']} confianza baja | "
               f"{d['not_found']} no encontradas | {d['errors']} errores")
    typer.echo(f"  cache: {d['cache']['hits']} hits / {d['cache']['misses']} misses "
               f"({d['cache']['hit_rate']:.0%})")
    typer.echo(f"  {d['elapsed_s']}s ({d['rows_per_s']} filas/s)")
    typer.echo(f"  salida: {output}")

    report_path = Path(str(output).rsplit(".", 1)[0] + ".geocode.report.json")
    report_path.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    typer.echo(f"  report: {report_path}")


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
    typer.echo(f"  IA: {'on' if cfg.ai_enabled else 'off'} | PBF dir: {cfg.pbf_dir or '(sin configurar)'}")
    if workers > 1:
        typer.echo("  aviso: con workers > 1 los jobs no se comparten entre procesos")
    uvicorn.run("smart_import.api:app", host=host, port=port, reload=reload,
                workers=workers if not reload else 1)


@app.command()
def extract(
    input: Path = typer.Option(..., "--input", "-i", exists=True, help="CSV ya normalizado."),
    output: Path = typer.Option(..., "--output", "-o"),
    column: str = typer.Option("address", help="Columna que mezcla varios campos."),
    fields: str = typer.Option("customer_name,address,phone", help="Campos a separar."),
    max_rows: int = typer.Option(None, help="Tope de filas (default: SMART_IMPORT_EXTRACT_MAX_ROWS)."),
) -> None:
    """Separa una columna que mezcla nombre/direccion/telefono usando el modelo.

    Es la UNICA operacion que usa IA. Cuesta ~1 s por fila en CPU, asi que tiene
    tope de filas y conviene correrla en segundo plano.
    """
    import csv as _csv

    from .extraction import CompositeExtractor

    cfg = Config.from_env()
    names = tuple(f.strip() for f in fields.split(",") if f.strip())
    limit = max_rows or cfg.extract_max_rows

    with open(input, encoding="utf-8", newline="") as fh:
        rows = list(_csv.DictReader(fh))
    if not rows:
        raise typer.BadParameter(f"{input} no tiene filas")
    if column not in rows[0]:
        raise typer.BadParameter(f"la columna '{column}' no existe. Hay: {list(rows[0])}")

    texts = [r.get(column) or "" for r in rows]
    typer.echo(f"  modelo: {cfg.model} en {cfg.device}")
    typer.echo(f"  separando '{column}' en {list(names)} ({min(len(texts), limit)} filas)...")

    def progress(done, res):
        typer.echo(f"    {done} filas... ({res.extracted} extraidas, {res.failed} fallidas)")

    extractor = CompositeExtractor(cfg, fields=names)
    result = extractor.run(texts, max_rows=limit, progress=progress)

    columns = list(rows[0])
    for name in names:
        if name not in columns:
            columns.append(name)
    for row, values in zip(rows, result.values):
        for name in names:
            if values.get(name):
                row[name] = values[name]

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8", newline="") as fh:
        writer = _csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})

    d = result.as_dict()
    typer.echo(f"  {d['extracted']}/{d['rows']} filas separadas | {d['failed']} fallidas")
    typer.echo(f"  {d['elapsed_s']}s ({d['seconds_per_row']}s por fila)")
    for w in d["warnings"]:
        typer.echo(f"  aviso: {w}")
    typer.echo(f"  salida: {output}")


@app.command()
def warmup() -> None:
    """Baja el modelo al cache y verifica que el servicio puede arrancar.

    Se corre ANTES de servir para que el primer usuario no pague la descarga.
    Sale con codigo 0 aunque el modelo no cargue: no poder precargar no es
    motivo para impedir que el servicio arranque.
    """
    from .logging_setup import stage
    from .geocoding.pbf_registry import PbfRegistry
    from .logging_setup import get_logger

    log = get_logger("warmup")
    cfg = Config.from_env()

    stage(log, "HTTP", "verificando el despliegue",
          geocoding="on" if cfg.geocoding_enabled else "off",
          ia="on" if cfg.ai_enabled else "off")

    schema_dir = Path(cfg.schema_dir)
    schemas = sorted(p.stem for p in schema_dir.glob("*.json")) if schema_dir.exists() else []
    stage(log, "HTTP", "schemas", encontrados=",".join(schemas) or "NINGUNO")
    if not schemas:
        typer.echo(f"  aviso: no hay schemas en {schema_dir}", err=True)

    if cfg.geocoding_enabled:
        registry = PbfRegistry.scan(cfg.pbf_dir) if cfg.pbf_dir else PbfRegistry([])
        stage(log, "GEOCODE", "PBF disponibles", directorio=cfg.pbf_dir or "(sin configurar)",
              cantidad=len(registry.entries), con_bbox=len(registry.with_bbox()))
        if not registry.entries:
            typer.echo("  aviso: geocoding habilitado pero no se ve ningun .osm.pbf. "
                       "Revisa el mount de /data/pbf.", err=True)

    if not cfg.ai_enabled:
        stage(log, "DONE", "warmup listo (IA deshabilitada, no hay modelo que bajar)")
        return

    try:
        from .models.loader import load
        stage(log, "EXTRACT", "descargando/cargando modelo",
              modelo=cfg.model, device=cfg.device)
        loaded = load(cfg.model, cfg.device)
        stage(log, "DONE", "warmup listo", modelo=cfg.model,
              carga=f"{loaded.load_seconds:.1f}s")
    except Exception as exc:
        typer.echo(f"  aviso: no se pudo precargar el modelo ({exc}). "
                   "El servicio arranca igual; extract va a devolver 503.", err=True)
