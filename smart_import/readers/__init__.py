"""Punto de entrada unico de lectura: `read_any(path)` devuelve un Table."""
from __future__ import annotations

from pathlib import Path

from .base import FileMeta, Table, detect_format, is_blank
from . import excel_reader, json_reader, text_reader

__all__ = ["read_any", "Table", "FileMeta", "detect_format", "is_blank"]


def read_any(path: str | Path, max_rows: int | None = None, sheet: str | None = None,
             config=None) -> Table:
    fmt = detect_format(path)
    if fmt == "xlsx":
        return excel_reader.read_xlsx(path, max_rows=max_rows, sheet=sheet)
    if fmt == "xls":
        return excel_reader.read_xls(path, max_rows=max_rows, sheet=sheet)
    if fmt == "json":
        return json_reader.read(path, max_rows=max_rows)
    return text_reader.read(path, max_rows=max_rows, fmt=fmt, config=config)
