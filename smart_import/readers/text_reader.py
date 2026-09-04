"""CSV / TSV / TXT: encoding, delimiter y fila de header se detectan."""
from __future__ import annotations

import csv
from pathlib import Path

from .base import FileMeta, Table, choose_header_row, dedupe_columns, is_blank

CANDIDATE_DELIMITERS = [",", ";", "\t", "|"]


def detect_encoding(path: str | Path, sample_bytes: int = 64 * 1024) -> str:
    with open(path, "rb") as fh:
        raw = fh.read(sample_bytes)
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(raw).best()
        if best and best.encoding:
            return best.encoding
    except Exception:
        pass
    return "cp1252"


def detect_delimiter(text: str) -> str:
    """El delimiter que parte las lineas en la mayor cantidad de campos, de forma
    CONSISTENTE. csv.Sniffer solo no alcanza: con direcciones entrecomilladas que
    contienen comas confunde ';' con ','."""
    lines = [ln for ln in text.splitlines()[:50] if ln.strip()]
    if not lines:
        return ","
    best, best_score = ",", float("-inf")
    for delim in CANDIDATE_DELIMITERS:
        counts = []
        for ln in lines[:20]:
            try:
                counts.append(len(next(csv.reader([ln], delimiter=delim))))
            except Exception:
                counts.append(1)
        if not counts:
            continue
        fields = max(set(counts), key=counts.count)
        if fields < 2:
            continue
        consistency = counts.count(fields) / len(counts)
        score = fields * consistency + consistency * 2
        if score > best_score:
            best, best_score = delim, score
    return best


def read(path: str | Path, max_rows: int | None = None, fmt: str = "csv") -> Table:
    p = Path(path)
    encoding = detect_encoding(p)
    with open(p, "r", encoding=encoding, errors="replace", newline="") as fh:
        text = fh.read()

    delimiter = "\t" if fmt == "tsv" else detect_delimiter(text)
    matrix = list(csv.reader(text.splitlines(), delimiter=delimiter))
    matrix = [r for r in matrix if any(not is_blank(c) for c in r)]

    header_row = choose_header_row(matrix)
    columns = dedupe_columns(matrix[header_row] if matrix else [])
    width = len(columns)

    rows: list[tuple] = []
    for raw in matrix[header_row + 1:]:
        if all(is_blank(c) for c in raw):
            continue
        vals = [None if is_blank(c) else str(c).strip() for c in raw[:width]]
        vals += [None] * (width - len(vals))
        rows.append(tuple(vals))
        if max_rows and len(rows) >= max_rows:
            break

    meta = FileMeta(
        path=str(p), format=fmt, size_bytes=p.stat().st_size,
        encoding=encoding, delimiter=delimiter,
        header_row=header_row, preamble_rows=header_row,
    )
    if header_row:
        meta.notes.append(f"{header_row} fila(s) de preambulo descartadas antes del header")
    return Table(meta=meta, columns=columns, rows=rows)
