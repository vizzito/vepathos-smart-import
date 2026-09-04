"""Pipeline completo: read -> detect -> normalize -> assemble -> emit.

Es la unica pieza que conoce el orden de las etapas; el CLI y (mas adelante) el
consumer de cola llaman aca.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .emit import write_flat_csv, write_flat_xlsx, write_nested_json, write_report
from .mapping import build_mapper
from .mapping.base import ColumnMapping, MappingResult
from .normalization.row_normalizer import (
    STATUS_INVALID, STATUS_NEEDS_GEOCODE, STATUS_OK, RowNormalizer,
)
from .readers import read_any
from .schemas import TargetSchema
from .assemble import assemble


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

    table = read_any(src, max_rows=cfg.max_rows, sheet=sheet)
    timer.mark("read")

    mapper = build_mapper(cfg)
    mapping = mapper.detect(table, schema)
    timer.mark("detect")
    if manual_mapping:
        mapping = apply_manual_mapping(mapping, manual_mapping)

    outcome = RowNormalizer(schema, phone_region=phone_region,
                            derive_volume=derive_volume).run(table, mapping)
    timer.mark("normalize")

    deliveries, group_warnings = assemble(outcome.rows, schema)
    outcome.warnings.extend(group_warnings)
    timer.mark("assemble")

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
        "rows_with_issues": sum(1 for r in outcome.rows if r.issues),
        "rejected_coordinates": sum(
            1 for r in outcome.rows if any("fuera de rango" in i for i in r.issues)),
        "output_columns": outcome.targets_present,
        **mapping.as_dict(),
        "warnings": mapping.warnings + outcome.warnings,
        "row_issues": [
            {"row": r.index, "delivery_id": r.values.get("delivery_id"),
             "status": r.status, "issues": r.issues}
            for r in outcome.rows if r.issues
        ][:200],
    }
    report["processing_times"] = timer.as_dict(time.perf_counter() - t_start)

    result = ImportResult(table=table, mapping=mapping, outcome=outcome,
                          deliveries=deliveries, report=report)

    if output_path:
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

    return result
