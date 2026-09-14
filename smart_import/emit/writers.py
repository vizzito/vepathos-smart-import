"""Salidas: plano (CSV/XLSX) y anidado (JSON), mas el report."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..atomic import output_path
from ..normalization.row_normalizer import NormalizedRow
from ..normalization.values import render

DIAGNOSTIC_COLUMNS = ("row_status", "row_issues")


def write_flat_csv(path: str | Path, columns: list[str], rows: list[NormalizedRow],
                   diagnostics: bool = False) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    header = list(columns) + (list(DIAGNOSTIC_COLUMNS) if diagnostics else [])
    with output_path(p) as temp, open(temp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        for row in rows:
            line = [render(row.values.get(c)) for c in columns]
            if diagnostics:
                line += [row.status, " | ".join(row.messages)]
            w.writerow(line)
    return p


def write_flat_xlsx(path: str | Path, columns: list[str], rows: list[NormalizedRow],
                    sheet_name: str = "deliveries", diagnostics: bool = False) -> Path:
    from openpyxl import Workbook

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook(write_only=True)                   # write_only: no arma la hoja en RAM
    ws = wb.create_sheet(title=sheet_name)
    ws.append(list(columns) + (list(DIAGNOSTIC_COLUMNS) if diagnostics else []))
    for row in rows:
        line: list[Any] = [row.values.get(c) for c in columns]
        if diagnostics:
            line += [row.status, " | ".join(row.messages)]
        ws.append(line)
    with output_path(p) as temp:
        wb.save(temp)
    return p


def write_nested_json(path: str | Path, deliveries: list[dict],
                      version: int = 1) -> Path:
    """Emite el JSON nested que ya consume el optimizador / router.

    Forma canónica (ver examples/vepathos-golden/):
      {"version": 1, "addresses": [{delivery_id, lat, lng, address, zone, packages: [...]}]}
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"version": version, "addresses": deliveries},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return p


def write_report(path: str | Path, report: dict) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with output_path(p) as temp:
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p
