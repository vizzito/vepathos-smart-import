"""Estructura comun de lectura.

Las filas se guardan como tuplas alineadas a `columns` (no dicts): para 50k filas
x 20 columnas eso es ~3x menos memoria que una lista de dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


@dataclass
class FileMeta:
    path: str
    format: str                      # csv | tsv | txt | xlsx | xls | json
    size_bytes: int = 0
    encoding: str | None = None
    delimiter: str | None = None
    sheet: str | None = None
    sheets: list[str] = field(default_factory=list)
    header_row: int = 0              # 0-based, dentro del archivo/hoja
    preamble_rows: int = 0
    #: solo para texto: "tabular" | "free_text". None = no aplica (xlsx/json)
    text_mode: str | None = None
    #: por que se decidio ese modo (evidencia estructural)
    text_mode_evidence: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def is_free_text(self) -> bool:
        return self.text_mode == "free_text"


@dataclass
class Table:
    meta: FileMeta
    columns: list[str]
    rows: list[tuple]

    def __len__(self) -> int:
        return len(self.rows)

    def iter_dicts(self) -> Iterator[dict[str, Any]]:
        cols = self.columns
        for row in self.rows:
            yield dict(zip(cols, row))

    def sample(self, n: int) -> list[dict[str, Any]]:
        """Filas representativas: prioriza las que tienen mas celdas no vacias.

        Un archivo cuyas primeras filas estan medio vacias enganaria al mapper
        (y al modelo) si tomaramos head(n) a secas.
        """
        if not self.rows:
            return []
        scored = sorted(
            range(len(self.rows)),
            key=lambda i: (-_filled(self.rows[i]), i),
        )
        chosen = sorted(scored[: min(n, len(self.rows))])
        cols = self.columns
        return [dict(zip(cols, self.rows[i])) for i in chosen]

    def column_values(self, name: str, limit: int | None = None) -> list[Any]:
        try:
            idx = self.columns.index(name)
        except ValueError:
            return []
        rows = self.rows if limit is None else self.rows[:limit]
        return [r[idx] if idx < len(r) else None for r in rows]


def _filled(row: tuple) -> int:
    return sum(1 for v in row if v is not None and str(v).strip() != "")


def is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def detect_format(path: str | Path) -> str:
    """Formato por extension, con sniffing de contenido como desempate."""
    p = Path(path)
    ext = p.suffix.lower()
    by_ext = {
        ".csv": "csv", ".tsv": "tsv", ".txt": "txt",
        ".xlsx": "xlsx", ".xlsm": "xlsx", ".xls": "xls", ".json": "json",
    }
    fmt = by_ext.get(ext)

    with open(p, "rb") as fh:
        head = fh.read(8)
    if head[:4] == b"PK\x03\x04":
        return "xlsx"                                  # zip => OOXML
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "xls"                                   # OLE2 => xls viejo
    if fmt in {"xlsx", "xls"}:
        # extension miente y no es ni zip ni OLE2 -> es texto
        fmt = None
    if fmt:
        return fmt

    with open(p, "rb") as fh:
        probe = fh.read(4096).lstrip()
    if probe[:1] in (b"{", b"["):
        return "json"
    return "txt"


def choose_header_row(matrix: list[list[Any]], max_scan: int = 12) -> int:
    """Indice de la fila que parece el header.

    Los exports de cliente suelen traer titulo/logo/fecha antes de la tabla. Se
    puntua cada fila: celdas no vacias, distintas, textuales y cortas.
    """
    best_idx, best_score = 0, float("-inf")
    for i, row in enumerate(matrix[:max_scan]):
        cells = [c for c in row if not is_blank(c)]
        if len(cells) < 2:
            continue
        texts = [str(c).strip() for c in cells]
        distinct = len(set(t.lower() for t in texts)) / len(texts)
        numeric = sum(1 for t in texts if _looks_numeric(t)) / len(texts)
        short = sum(1 for t in texts if len(t) <= 40) / len(texts)
        density = len(cells) / max(1, len(row))
        score = density * 2 + distinct * 2 + short - numeric * 3
        # una fila de header tiene, debajo, filas con al menos tantas celdas
        below = matrix[i + 1: i + 4]
        if below and max((sum(1 for c in r if not is_blank(c)) for r in below), default=0) < 2:
            score -= 2
        if score > best_score:
            best_idx, best_score = i, score
    return best_idx


def _looks_numeric(text: str) -> bool:
    t = text.replace(",", ".").replace(" ", "")
    if not t:
        return False
    try:
        float(t)
        return True
    except ValueError:
        return False


def dedupe_columns(names: list[Any]) -> list[str]:
    """Nombres unicos y no vacios; las columnas sin nombre quedan como col_N."""
    out, seen = [], {}
    for i, raw in enumerate(names):
        name = "" if raw is None else str(raw).strip()
        if not name:
            name = f"col_{i + 1}"
        base, n = name, seen.get(name.lower(), 0)
        if n:
            name = f"{base}_{n + 1}"
        seen[base.lower()] = n + 1
        out.append(name)
    return out
