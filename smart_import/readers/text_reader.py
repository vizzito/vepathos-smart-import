"""CSV / TSV / TXT: encoding, delimiter y fila de header se detectan."""
from __future__ import annotations

import csv
import re
from pathlib import Path

from .base import FileMeta, Table, choose_header_row, dedupe_columns, is_blank

CANDIDATE_DELIMITERS = [",", ";", "\t", "|"]

# 1) Ana… / 12. Juan… / 3- María…  y tambien viñetas: - Ana… / * Juan… / • Maria…
# Las listas de WhatsApp casi siempre usan guion, no numero. Sin la viñeta, el
# texto cae al lector delimitado y se parte por la primera coma de cada linea.
_NUMBERED_DELIVERY = re.compile(r"^\s*(?:\d+[\)\.\-]|[-*•·])\s+\S")
_FOOTERISH = re.compile(
    r"^\s*(gracias|thanks|atte\.?|saludos|despacho|team|equipo)\b",
    re.IGNORECASE,
)


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


def looks_like_numbered_delivery_list(text: str) -> bool:
    """Paste tipo mail: '1) Nombre <tel> → calle…' (una entrega por línea)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return False
    numbered = [ln for ln in lines if _NUMBERED_DELIVERY.match(ln)]
    if len(numbered) < 3:
        return False
    return len(numbered) / len(lines) >= 0.25


def _read_numbered_delivery_list(
    path: Path, text: str, encoding: str, fmt: str, max_rows: int | None,
) -> Table:
    """Una columna 'info_cliente' = línea completa (no partir por comas)."""
    rows: list[tuple] = []
    skipped_preamble = 0
    skipped_footer = 0
    seen_delivery = False
    for ln in text.splitlines():
        raw = ln.strip()
        if not raw:
            continue
        if _NUMBERED_DELIVERY.match(raw):
            seen_delivery = True
            rows.append((raw,))
            if max_rows and len(rows) >= max_rows:
                break
            continue
        if not seen_delivery:
            skipped_preamble += 1
            continue
        if _FOOTERISH.match(raw) or len(raw) < 40:
            skipped_footer += 1
            continue
        rows.append((raw,))

    columns = ["info_cliente"]
    meta = FileMeta(
        path=str(path), format=fmt, size_bytes=path.stat().st_size,
        encoding=encoding, delimiter=None,
        header_row=0, preamble_rows=skipped_preamble,
    )
    meta.notes.append(
        f"lista numerada: {len(rows)} entrega(s) como columna unica "
        f"(preambulo={skipped_preamble}, pie={skipped_footer})"
    )
    return Table(meta=meta, columns=columns, rows=rows)


def read(path: str | Path, max_rows: int | None = None, fmt: str = "csv") -> Table:
    p = Path(path)
    encoding = detect_encoding(p)
    with open(p, "r", encoding=encoding, errors="replace", newline="") as fh:
        text = fh.read()

    # Pastes de despacho: NO usar delimiter ',' (rompe "calle 100, CABA | nota")
    if fmt in ("txt", "csv") and looks_like_numbered_delivery_list(text):
        return _read_numbered_delivery_list(p, text, encoding, fmt, max_rows)

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
