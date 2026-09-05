"""Pipeline completo: read -> detect -> normalize -> assemble -> emit.

Es la unica pieza que conoce el orden de las etapas; el CLI y (mas adelante) el
consumer de cola llaman aca.
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
    table = read_any(src, max_rows=cfg.max_rows, sheet=sheet)
    timer.mark("read")
    meta = table.meta
    stage(logger, "READ", "", formato=meta.format, encoding=meta.encoding,
          delimiter=repr(meta.delimiter) if meta.delimiter else None,
          hoja=meta.sheet, header_row=meta.header_row,
          filas=len(table), columnas=len(table.columns), t=f"{timer.marks['read']}s")
    for note in meta.notes:
        detail(logger, note)

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

    stage(logger, "NORMALIZE", "aplicando el mapping a todas las filas",
          filas=len(table), region_telefono=phone_region)
    outcome = RowNormalizer(schema, phone_region=phone_region,
                            derive_volume=derive_volume).run(table, mapping)
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

    stage(logger, "ASSEMBLE", f"agrupando filas por {schema.group_by}")
    deliveries, group_warnings = assemble(outcome.rows, schema)
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
