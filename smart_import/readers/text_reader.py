"""CSV / TSV / TXT: encoding, delimiter y fila de header se detectan."""
from __future__ import annotations

import csv
import re
from pathlib import Path

from ..detection.text_mode import TextMode, classify_text_mode
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


def _read_free_text(path: Path, text: str, encoding: str, fmt: str,
                    decision) -> Table:
    """Documento COMPLETO en una sola celda.

    Nada de header, nada de preambulo descartado, nada partido por comas: lo que
    sigue (segmentacion + clasificacion + extraccion) necesita el texto entero.
    """
    meta = FileMeta(
        path=str(path), format=fmt, size_bytes=path.stat().st_size,
        encoding=encoding, delimiter=None, header_row=0, preamble_rows=0,
        text_mode=TextMode.FREE_TEXT.value, text_mode_evidence=decision.as_dict(),
    )
    lines = sum(1 for ln in text.splitlines() if ln.strip())
    meta.notes.append(
        f"texto libre: el documento se preserva entero ({lines} linea(s), "
        f"{len(text)} caracteres). Motivo: {decision.reasons[0]}")
    return Table(meta=meta, columns=["document"], rows=[(text,)])


def read(path: str | Path, max_rows: int | None = None, fmt: str = "csv",
         config=None) -> Table:
    p = Path(path)
    encoding = detect_encoding(p)
    with open(p, "r", encoding=encoding, errors="replace", newline="") as fh:
        text = fh.read()

    # Un .txt no es una tabla porque tenga comas. Se exige evidencia estructural.
    decision = None
    if fmt == "txt":
        decision = classify_text_mode(
            text, fmt=fmt,
            min_consistency=getattr(config, "text_tabular_min_consistency", 0.80),
            min_lines=getattr(config, "text_tabular_min_lines", 3),
            max_prose_ratio=getattr(config, "text_max_prose_ratio", 0.20),
            min_header_score=getattr(config, "text_min_header_score", 0.50),
        )
        if decision.mode is TextMode.FREE_TEXT:
            return _read_free_text(p, text, encoding, fmt, decision)

    # Paste de despacho guardado como .csv: NO partir por ',' ("calle 100, CABA")
    if fmt == "csv" and looks_like_numbered_delivery_list(text):
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
        text_mode=TextMode.TABULAR.value if fmt in ("txt", "csv", "tsv") else None,
        text_mode_evidence=decision.as_dict() if decision else {},
    )
    if header_row:
        meta.notes.append(f"{header_row} fila(s) de preambulo descartadas antes del header")
    return Table(meta=meta, columns=columns, rows=rows)
