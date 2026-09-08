"""Pipeline completo: read -> detect -> normalize -> assemble -> emit.

Es la unica pieza que conoce el orden de las etapas; el CLI y la API llaman aca.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .logging_setup import detail, get_logger, stage
from .emit import write_flat_csv, write_flat_xlsx, write_nested_json, write_report
from .mapping import build_mapper
from .mapping.base import ColumnMapping, MappingResult
from .normalization.row_normalizer import (
    STATUS_IGNORED, STATUS_INVALID, STATUS_NEEDS_GEOCODE, STATUS_OK, RowNormalizer,
)
from .readers import read_any
from .schemas import TargetSchema
from .assemble import assemble
from .locality import detect_locality

logger = get_logger("pipeline")


class Timer:
    def __init__(self) -> None:
        self.marks: dict[str, float] = {}
        self._t0 = time.perf_counter()

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self.marks[name] = round(now - self._t0, 4)
        self._t0 = now

    def as_dict(self, total: float) -> dict[str, float]:
        return {**self.marks, "total": round(total, 4)}


@dataclass
class ImportResult:
    table: Any = None
    mapping: MappingResult = field(default_factory=MappingResult)
    outcome: Any = None
    deliveries: list[dict] = field(default_factory=list)
    report: dict = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)


def apply_manual_mapping(mapping: MappingResult, overrides: dict[str, str]) -> MappingResult:
    """El usuario corrige el mapping sugerido; sus decisiones son finales."""
    for column, target in overrides.items():
        if target is None or target == "":
            mapping.mapping.pop(column, None)
            if column not in mapping.unmapped:
                mapping.unmapped.append(column)
            continue
        for other, m in list(mapping.mapping.items()):
            if m.target == target and other != column:
                mapping.mapping.pop(other)          # un target, una sola columna
                mapping.unmapped.append(other)
        mapping.mapping[column] = ColumnMapping(column, target, 1.0, "manual", "corregido por el usuario")
        if column in mapping.unmapped:
            mapping.unmapped.remove(column)
    mapping.ambiguous = [a for a in mapping.ambiguous if a["column"] not in overrides]
    return mapping


def run_normalize(
    input_path: str | Path,
    schema_path: str | Path,
    output_path: str | Path | None = None,
    emit: tuple[str, ...] = ("flat",),
    config: Config | None = None,
    manual_mapping: dict[str, str] | None = None,
    phone_region: str | None = None,
    diagnostics: bool = False,
    sheet: str | None = None,
    derive_volume: bool = False,
    service_date=None,
    timezone: str | None = None,
    depot_city: str | None = None,
    depot_region: str | None = None,
    depot_country: str | None = None,
    expand_composite: bool = True,
) -> ImportResult:
    cfg = config or Config.from_env()
    schema = TargetSchema.load(schema_path)
    timer = Timer()
    t_start = time.perf_counter()

    src = Path(input_path)
    size_mb = src.stat().st_size / (1024 * 1024)
    if size_mb > cfg.max_file_mb:
        raise ValueError(f"archivo de {size_mb:.1f} MB supera el limite de {cfg.max_file_mb} MB "
                         f"(SMART_IMPORT_MAX_FILE_MB)")

    stage(logger, "READ", "abriendo", archivo=src.name, tamano=f"{size_mb:.2f}MB")
    table = read_any(src, max_rows=cfg.max_rows, sheet=sheet, config=cfg)
    timer.mark("read")
    meta = table.meta
    stage(logger, "READ", "", formato=meta.format, encoding=meta.encoding,
          modo=meta.text_mode,
          delimiter=repr(meta.delimiter) if meta.delimiter else None,
          hoja=meta.sheet, header_row=meta.header_row,
          filas=len(table), columnas=len(table.columns), t=f"{timer.marks['read']}s")
    for note in meta.notes:
        detail(logger, note)

    # El documento crudo se guarda ANTES de extraer: `table` se reemplaza por
    # las filas y el encabezado ('Deliveries for today (Miami)') se pierde.
    document_text = (table.rows[0][0] if meta.is_free_text and table.rows else None)

    extraction = None
    if meta.is_free_text:
        # El archivo entero es texto libre: segmentar -> clasificar -> extraer.
        # No hay columnas que mapear, asi que el mapper no corre.
        table, mapping, extraction = _extract_free_text(
            table, schema, cfg, phone_region, service_date, timezone,
            depot_city=depot_city, depot_region=depot_region,
            depot_country=depot_country)
        timer.mark("detect")
    else:
        stage(logger, "DETECT", "resolviendo columnas contra", schema=schema.name)
        mapper = build_mapper(cfg)
        mapping = mapper.detect(table, schema)
        timer.mark("detect")
    for column, m in sorted(mapping.mapping.items(), key=lambda kv: -kv[1].confidence):
        flag = "" if m.confidence >= cfg.auto_accept_threshold else "   <-- REVISAR"
        detail(logger, f"{column:<24} -> {m.target:<18} {m.confidence:.2f} {m.method}{flag}")
    for column in mapping.unmapped:
        detail(logger, f"{column:<24} -> (sin mapear)")
    stage(logger, "DETECT", "", mapeadas=len(mapping.mapping),
          sin_mapear=len(mapping.unmapped), a_revisar=len(mapping.ambiguous),
          ia="no", t=f"{timer.marks['detect']}s")
    for warning in mapping.warnings:
        stage(logger, "WARN", warning, level=logging.WARNING)

    if manual_mapping:
        stage(logger, "DETECT", "aplicando correcciones del usuario",
              columnas=len(manual_mapping))
        mapping = apply_manual_mapping(mapping, manual_mapping)

    if expand_composite and extraction is None and not meta.is_free_text:
        # Una columna que mezcla nombre/direccion/telefono se separa con reglas.
        # Solo esa columna: el resto del archivo sigue el camino rapido.
        # Tras geocode el CSV YA es Vepathos enriquecido: expand_composite=False.
        table, mapping = _expand_composite_column(
            table, mapping, schema, cfg, phone_region, service_date, timezone)

    stage(logger, "NORMALIZE", "aplicando el mapping a todas las filas",
          filas=len(table), region_telefono=phone_region)
    outcome = RowNormalizer(schema, phone_region=phone_region,
                            derive_volume=derive_volume,
                            timezone=timezone).run(table, mapping)
    timer.mark("normalize")
    counts_ = outcome.counts()
    stage(logger, "NORMALIZE", "",
          con_coordenadas=counts_.get(STATUS_OK, 0),
          necesitan_geocoding=counts_.get(STATUS_NEEDS_GEOCODE, 0),
          invalidas=counts_.get(STATUS_INVALID, 0),
          ignoradas=counts_.get(STATUS_IGNORED, 0) or None,
          filas_vacias_descartadas=outcome.skipped_empty or None,
          t=f"{timer.marks['normalize']}s")
    for warning in outcome.warnings:
        stage(logger, "WARN", warning, level=logging.WARNING)
    for issue in [r for r in outcome.rows if r.issues][:5]:
        detail(logger, f"fila {issue.index} ({issue.values.get('delivery_id') or 's/id'}): "
                       f"{issue.issues[0]}")

    # De que ciudad habla el archivo. No decide nada: el depot del selector
    # sigue mandando. La UI usa esto para pre-cargar / preguntar antes de
    # geocodificar, porque un depot de otra ciudad no devuelve NINGUN pin.
    locality = detect_locality(rows=[r.values for r in outcome.rows],
                               document=document_text)
    best = locality.best
    if best is None:
        stage(logger, "LOCALITY", "no se detecto ciudad en el archivo: "
                                  "la UI tiene que pedirla antes de geocodificar")
    else:
        stage(logger, "LOCALITY", "ciudad del archivo",
              ciudad=best.city, region=best.region, pais=best.country,
              confianza=f"{best.confidence:.2f}",
              filas=f"{best.support}/{locality.rows_total}" if best.support else None,
              fuentes=",".join(best.sources),
              confirmar="si" if locality.needs_user_input else "no")
        if locality.reason:
            detail(logger, f"localidad: {locality.reason}")
        for other in locality.candidates[1:4]:
            detail(logger, f"localidad alternativa: {other.city} "
                           f"({other.confidence:.2f}, {','.join(other.sources)})")

    stage(logger, "ASSEMBLE", f"agrupando filas por {schema.group_by}")
    # `weight_kg` es el peso de UN bulto, venga de donde venga: el extractor de
    # texto libre ya reparte lo que la frase declaro como total. Repartirlo aca
    # otra vez rompia el round-trip (el flat se relee como tabular despues de
    # geocodificar, y el peso se multiplicaba por la cantidad en cada vuelta).
    deliveries, group_warnings = assemble(outcome.rows, schema,
                                          weight_is_total=False)
    outcome.warnings.extend(group_warnings)
    timer.mark("assemble")
    sin_bultos = sum(1 for d in deliveries if not d["packages"])
    stage(logger, "ASSEMBLE", "", entregas=len(deliveries),
          bultos=sum(len(d["packages"]) for d in deliveries),
          entregas_sin_bultos=sin_bultos or None, t=f"{timer.marks['assemble']}s")
    for warning in group_warnings:
        stage(logger, "WARN", warning, level=logging.WARNING)

    counts = outcome.counts()
    report = {
        "input": {
            "path": str(src), "format": table.meta.format, "size_bytes": table.meta.size_bytes,
            "encoding": table.meta.encoding, "delimiter": table.meta.delimiter,
            "sheet": table.meta.sheet, "header_row": table.meta.header_row,
            "columns": table.columns, "notes": table.meta.notes,
        },
        "schema": schema.name,
        "text_mode": table.meta.text_mode,
        # Evidencia de ciudad/region del archivo. La UI la usa para confirmar
        # antes de geocodificar; el backend no la aplica por su cuenta.
        "locality": locality.as_dict(),
        "rows_input": len(table), "rows_output": len(outcome.rows),
        "skipped_empty_rows": outcome.skipped_empty,
        "deliveries": len(deliveries),
        "packages": sum(len(d["packages"]) for d in deliveries),
        "valid_rows": counts.get(STATUS_OK, 0),
        "needs_geocode": counts.get(STATUS_NEEDS_GEOCODE, 0),
        "invalid_rows": counts.get(STATUS_INVALID, 0),
        "ignored_rows": counts.get(STATUS_IGNORED, 0),
        "rows_with_issues": sum(1 for r in outcome.rows if r.issues),
        "rejected_coordinates": sum(
            1 for r in outcome.rows if any("fuera de rango" in m for m in r.messages)),
        "output_columns": outcome.targets_present,
        **mapping.as_dict(),
        "warnings": mapping.warnings + outcome.warnings,
        "row_issues": [
            {"row": r.index, "delivery_id": r.values.get("delivery_id"),
             "status": r.status,
             "issues": [i.as_dict() for i in r.issues],
             "messages": r.messages,
             "fields": r.problem_fields}
            for r in outcome.rows if r.issues
        ][:200],
    }
    report["processing_times"] = timer.as_dict(time.perf_counter() - t_start)
    report["extraction"] = (extraction.as_dict() if extraction is not None
                            else {"deliveries": len(deliveries), "ignored": 0,
                                  "ai_calls": 0, "mode": "tabular"})
    report["ai_calls"] = report["extraction"].get("ai_calls", 0)

    result = ImportResult(table=table, mapping=mapping, outcome=outcome,
                          deliveries=deliveries, report=report)

    if output_path:
        stage(logger, "EMIT", "escribiendo salidas", formatos=",".join(emit))
        out = Path(output_path)
        stem = out.with_suffix("")
        if "flat" in emit:
            if out.suffix.lower() in (".xlsx", ".xlsm"):
                write_flat_xlsx(out, outcome.targets_present, outcome.rows,
                                sheet_name=schema.sheet_name, diagnostics=diagnostics)
            else:
                write_flat_csv(out, outcome.targets_present, outcome.rows, diagnostics=diagnostics)
            result.outputs["flat"] = str(out)
        if "nested" in emit:
            nested = Path(f"{stem}.nested.json")
            write_nested_json(nested, deliveries)
            result.outputs["nested"] = str(nested)
        report_path = Path(f"{stem}.report.json")
        write_report(report_path, report)
        result.outputs["report"] = str(report_path)
        for kind, path in result.outputs.items():
            detail(logger, f"{kind:<8} {path}")

    stage(logger, "DONE", "normalize terminado",
          total=f"{report['processing_times']['total']}s",
          revisar="si" if report["needs_review"] else "no")
    return result


# --------------------------------------------------------------- texto libre

def _extract_free_text(table, schema, cfg, phone_region, service_date,
                       timezone=None, depot_city=None, depot_region=None,
                       depot_country=None):
    """Documento completo -> filas del schema. Sin modelo, sin llamadas de IA."""
    from .extraction.context import ExtractionContext
    from .extraction.free_text import FreeTextExtractor, records_to_table
    from .normalization.address import infer_locality_tokens, iso_from_country_label

    document = table.rows[0][0] if table.rows else ""
    depot_iso = iso_from_country_label(depot_country)
    doc_iso = next(
        (iso_from_country_label(t) for t in infer_locality_tokens(document)
         if iso_from_country_label(t)),
        None,
    )
    # Depot o ciudad del texto ganan sobre un phone_region default (AR en Miami).
    inferred_iso = depot_iso or doc_iso
    if inferred_iso and (not phone_region or phone_region.upper() != inferred_iso):
        phone_region = inferred_iso
    context = ExtractionContext.from_config(
        cfg, phone_region=phone_region,
        city=depot_city, state=depot_region, country=depot_country,
        country_code=inferred_iso,
    )
    from .addresses import describe_parsers
    parsers = describe_parsers(cfg)
    stage(logger, "EXTRACT", "texto libre: segmentando y extrayendo con reglas",
          caracteres=len(document), region=context.phone_region or None,
          address_parser=parsers["active"],
          libpostal="on" if parsers.get("libpostal_as_enhancer") else "off")

    extractor = FreeTextExtractor(cfg, context, service_date=service_date,
                                  timezone=timezone, schema=schema)
    result = extractor.run_document(document)
    new_table, mapping = records_to_table(result.records, schema, table.meta)

    for block in result.tables:
        stage(logger, "EXTRACT", "bloque tabular embebido: se lee como tabla, "
                                 "no linea por linea",
              filas=block["rows"], delimiter=repr(block["delimiter"]),
              columnas=",".join(f"{c}->{t}" for c, t in block["mapped"].items()))

    counts = result.counts()
    stage(logger, "EXTRACT", "", segmentos=result.segments,
          entregas=len(result.records), ignorados=len(result.ignored),
          a_revisar=counts.get("needs_review", 0), llamadas_ia=result.ai_calls,
          t=f"{result.elapsed_s:.3f}s")
    # Resumen del enhancer: una linea, no una por direccion.
    stats = getattr(extractor.fields.address_parser, "stats", None)
    if callable(stats):
        s = stats()
        calls = s.get("enhancer_calls", 0)
        skips = s.get("enhancer_skips", 0)
        if calls == 0:
            stage(logger, "ADDRESS", "libpostal no hizo falta",
                  skip=skips)
        else:
            stage(logger, "ADDRESS", "resumen libpostal",
                  skip=f"{skips} ({s.get('skip_pct', 0):.0f}%)",
                  called=calls,
                  helped=s.get("helped", 0),
                  noop=s.get("noop", 0),
                  reasons=",".join(
                      f"{k}:{v}" for k, v in (s.get("by_reason") or {}).items()) or None)
            for example in (s.get("helped_examples") or [])[:3]:
                detail(logger, f"libpostal helped: {example}")
    for item in result.ignored[:5]:
        detail(logger, f"ignorado: {item['text'][:60]!r} -> "
                       f"{(item['reasons'] or ['sin evidencia'])[0]}")
    return new_table, mapping, result


def _expand_composite_column(table, mapping, schema, cfg, phone_region,
                             service_date, timezone=None):
    """Si el mapper marco una columna como 'mezcla varios campos', separarla."""
    from .extraction.context import ExtractionContext
    from .extraction.free_text import FreeTextExtractor, expand_free_text_column

    column = next(
        (
            col for col, m in mapping.mapping.items()
            if "varios campos" in (m.evidence or "")
            and m.target == "address"          # nunca expandir phone/name ya mapeados
        ),
        None,
    )
    if column is None:
        return table, mapping

    stage(logger, "EXTRACT", "columna con varios campos: separando con reglas",
          columna=column, filas=len(table))
    context = ExtractionContext.from_config(cfg, phone_region=phone_region)
    extractor = FreeTextExtractor(cfg, context, service_date=service_date,
                                  timezone=timezone)
    expanded = expand_free_text_column(table, column, extractor, schema)

    mapper = build_mapper(cfg)
    new_mapping = mapper.detect(expanded, schema)
    stage(logger, "EXTRACT", "", campos_nuevos=len(expanded.columns) - len(table.columns) + 1,
          t=f"{extractor.fields.stats.elapsed_s:.3f}s")
    return expanded, new_mapping
