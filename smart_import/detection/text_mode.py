"""TXT tabular vs TXT texto libre.

El problema que resuelve: un paste de WhatsApp tiene comas de lenguaje natural.
`csv.Sniffer` (y el scoring de delimiter a secas) las toma como separador, y el
documento queda partido en columnas *antes* de que ningun extractor lo vea: la
primera entrega desaparece tomada como header.

    delimiter=','  header_row=2  columns=2
    -> ['- Juan Lopez (1140011001) entrega en Palermo', ' la calle es Av. Santa Fe al 137.']

Una coma repetida NO es evidencia de tabla. Para declarar TABULAR hay que pasar
TODAS las puertas de abajo; con que falle una, el documento se preserva entero
como FREE_TEXT. Es asimetrico a proposito: romper un texto libre pierde datos,
tratar una tabla como texto libre solo cuesta un poco mas de parsing.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from enum import Enum

from ..resources import fold, label_set

CANDIDATE_DELIMITERS = (",", ";", "\t", "|")

# viñetas y numeracion: '- Ana', '* Ana', '1) Ana', '12. Ana', '3- Ana'
_BULLET = re.compile(r"^\s*(?:\d{1,3}[)\.\-:]|[-*•·–—])\s+\S")
# 'Hola chicos!' / 'Salutos,' / 'Despacho' -> primera palabra del saludo o despedida
_FIRST_WORDS = re.compile(r"^[\s\W]*([^\W\d_]+(?:\s+[^\W\d_]+)?)", re.UNICODE)
# una oracion: muchas palabras y puntuacion final. Una celda de CSV rara vez lo es.
_SENTENCE_END = re.compile(r"[.!?]['\")\]]*\s*$")
MIN_SENTENCE_WORDS = 12

#: minimo de lineas no vacias para que la consistencia signifique algo
MIN_LINES = 3
#: minimo de lineas que comparten el conteo modal de campos
MIN_CONSISTENT_LINES = 3
#: cuanto puede medir el header para seguir pareciendo un header
MAX_HEADER_CELL = 40
#: cuantas lineas se miran (un archivo de 50k filas no necesita mas)
SCAN_LINES = 200


class TextMode(str, Enum):
    TABULAR = "tabular"
    FREE_TEXT = "free_text"


@dataclass(frozen=True)
class TextModeDecision:
    mode: TextMode
    delimiter: str | None = None
    field_count: int = 0
    consistency: float = 0.0
    consistent_lines: int = 0
    lines_considered: int = 0
    prose_ratio: float = 0.0
    header_score: float = 0.0
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_free_text(self) -> bool:
        return self.mode is TextMode.FREE_TEXT

    def as_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "delimiter": self.delimiter,
            "field_count": self.field_count,
            "consistency": round(self.consistency, 3),
            "consistent_lines": self.consistent_lines,
            "lines_considered": self.lines_considered,
            "prose_ratio": round(self.prose_ratio, 3),
            "header_score": round(self.header_score, 3),
            "reasons": list(self.reasons),
        }


def _split(line: str, delimiter: str) -> list[str]:
    try:
        return next(csv.reader([line], delimiter=delimiter))
    except Exception:
        return [line]


def is_prose_line(line: str, locales: tuple[str, ...] | None = None) -> bool:
    """Viñeta/numeracion, o saludo/despedida del lexico.

    No mira comas ni longitud: son justamente las señales que confunden. Mira la
    FORMA de la linea, que es lo que distingue una lista escrita a mano de una fila.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if _BULLET.match(line):
        return True
    if (_SENTENCE_END.search(stripped)
            and len(stripped.split()) >= MIN_SENTENCE_WORDS):
        return True
    match = _FIRST_WORDS.match(stripped)
    if not match:
        return False
    head = fold(match.group(1))
    greetings = label_set("greetings", locales)
    closings = label_set("closings", locales)
    first = head.split()[0]
    return head in greetings or head in closings or first in greetings or first in closings


def header_score(cells: list[str]) -> float:
    """Que tan header parece una fila: celdas cortas, textuales y distintas."""
    texts = [c.strip() for c in cells if c and c.strip()]
    if len(texts) < 2:
        return 0.0
    distinct = len({t.casefold() for t in texts}) / len(texts)
    short = sum(1 for t in texts if len(t) <= MAX_HEADER_CELL) / len(texts)
    numeric = sum(1 for t in texts if _looks_numeric(t)) / len(texts)
    density = len(texts) / max(1, len(cells))
    # Una fila toda numerica NO es un header: es la primera fila de datos.
    return max(0.0, (distinct + short + density) / 3 - numeric * 0.6)


def _looks_numeric(text: str) -> bool:
    t = text.replace(",", ".").replace(" ", "")
    if not t:
        return False
    try:
        float(t)
        return True
    except ValueError:
        return False


def _best_delimiter(lines: list[str]) -> tuple[str | None, int, float, int]:
    """(delimiter, campos modales, consistencia, lineas consistentes) del mejor candidato."""
    best: tuple[str | None, int, float, int] = (None, 0, 0.0, 0)
    for delim in CANDIDATE_DELIMITERS:
        counts = [len(_split(ln, delim)) for ln in lines]
        modal = max(set(counts), key=counts.count)
        if modal < 2:
            continue
        hits = counts.count(modal)
        consistency = hits / len(counts)
        # mas campos con la misma consistencia gana; la consistencia manda
        score = consistency * 10 + modal
        if score > best[2] * 10 + best[1]:
            best = (delim, modal, consistency, hits)
    return best


def classify_text_mode(
    text: str,
    *,
    fmt: str = "txt",
    min_consistency: float = 0.80,
    min_lines: int = MIN_LINES,
    max_prose_ratio: float = 0.20,
    min_header_score: float = 0.50,
    locales: tuple[str, ...] | None = None,
) -> TextModeDecision:
    """Decide TABULAR o FREE_TEXT con evidencia estructural, no con una coma suelta."""
    lines = [ln for ln in text.splitlines()[:SCAN_LINES] if ln.strip()]
    reasons: list[str] = []

    if len(lines) < min_lines:
        return TextModeDecision(
            TextMode.FREE_TEXT, lines_considered=len(lines),
            reasons=(f"solo {len(lines)} linea(s) no vacia(s), hacen falta {min_lines}",))

    prose = sum(1 for ln in lines if is_prose_line(ln, locales))
    prose_ratio = prose / len(lines)

    delimiter, modal, consistency, hits = _best_delimiter(lines)
    if delimiter is None:
        return TextModeDecision(
            TextMode.FREE_TEXT, lines_considered=len(lines), prose_ratio=prose_ratio,
            reasons=("ningun delimiter parte las lineas en 2+ campos",))

    head = header_score(_split(lines[0], delimiter))

    if consistency < min_consistency:
        reasons.append(f"solo {consistency:.0%} de las lineas tienen {modal} campos "
                       f"con {delimiter!r} (hace falta {min_consistency:.0%})")
    if hits < MIN_CONSISTENT_LINES:
        reasons.append(f"solo {hits} linea(s) comparten el conteo de campos")
    if prose_ratio > max_prose_ratio:
        reasons.append(f"{prose_ratio:.0%} de las lineas son viñetas o saludos "
                       f"(tope {max_prose_ratio:.0%})")
    if head < min_header_score:
        reasons.append(f"la primera fila no parece un header (score {head:.2f} < "
                       f"{min_header_score:.2f})")

    mode = TextMode.FREE_TEXT if reasons else TextMode.TABULAR
    if not reasons:
        reasons.append(f"{hits}/{len(lines)} lineas con {modal} campos separados por "
                       f"{delimiter!r}, header plausible")

    return TextModeDecision(
        mode=mode,
        delimiter=delimiter if mode is TextMode.TABULAR else None,
        field_count=modal, consistency=consistency, consistent_lines=hits,
        lines_considered=len(lines), prose_ratio=prose_ratio, header_score=head,
        reasons=tuple(reasons),
    )
