"""Una tabla escondida adentro de un documento de texto libre.

La gente arma un mensaje y despues le pega abajo el export del sistema. El
documento entero no es una tabla —tiene saludo, lista a mano, despedida— asi que
`classify_text_mode` lo manda a FREE_TEXT, y hace bien: partirlo por comas
perderia la lista. Pero adentro hay 40 lineas que SI son una tabla, con header y
todo, y leerlas linea por linea tira el nombre, los bultos y el peso.

Este modulo busca esas corridas. Solo mira la FORMA:

    * lineas consecutivas que el mismo delimiter parte en la misma cantidad de
      campos (una linea que no coincide corta la corrida: nada de "consistencia
      del 80%", que es como se cuelan los falsos positivos),
    * al menos `MIN_TABLE_ROWS` filas de datos debajo del header,
    * la primera linea parece un header y no es una viñeta ni un saludo.

Que un header sea *de verdad* un header no lo puede decidir la forma: "1. Ana
Perez | Av. Corrientes 100 | 11 4000-1000" tiene 3 celdas cortas y distintas y
pasa cualquier heuristica estructural. Eso lo decide el schema, afuera de aca:
quien llame a `find_table_blocks` tiene que confirmar que el mapper reconoce las
columnas antes de tratar el bloque como tabla.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass

from .segmenter import _physical_lines
from .text_mode import CANDIDATE_DELIMITERS, header_score, is_prose_line

#: filas de datos minimas, sin contar el header. Con dos, cualquier par de
#: lineas con una coma en el mismo lugar se disfraza de tabla.
MIN_TABLE_ROWS = 3
#: menos de dos columnas no es una tabla, es una linea con un separador
MIN_TABLE_COLUMNS = 2
#: cuanto tiene que parecer header la primera linea de la corrida
MIN_HEADER_SCORE = 0.55


@dataclass(frozen=True)
class TableBlock:
    """Una corrida de lineas delimitadas, con su ubicacion exacta."""
    start: int
    end: int
    delimiter: str
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    #: span absoluto de cada fila de datos, para ordenar los registros despues
    row_spans: tuple[tuple[int, int], ...]
    header_score: float = 0.0

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)

    @property
    def lines(self) -> int:
        return len(self.rows) + 1

    def as_dict(self) -> dict:
        return {"span": [self.start, self.end], "delimiter": self.delimiter,
                "header": list(self.header), "rows": len(self.rows),
                "header_score": round(self.header_score, 3)}


def _split(line: str, delimiter: str) -> list[str]:
    try:
        return next(csv.reader([line], delimiter=delimiter))
    except Exception:
        return [line]


def _runs(lines, delimiter: str):
    """Corridas maximas de lineas con el MISMO conteo de campos.

    Una linea vacia o con otro conteo cierra la corrida. Es estricto a
    proposito: el precio de cortar de mas es leer unas lineas como texto libre;
    el de cortar de menos es comerse la lista escrita a mano de arriba.
    """
    run: list[tuple[int, int, str, list[str]]] = []
    width = 0
    for start, end, text in lines:
        cells = _split(text, delimiter) if text.strip() else []
        if len(cells) >= MIN_TABLE_COLUMNS and (not run or len(cells) == width):
            width = len(cells)
            run.append((start, end, text, cells))
            continue
        if len(run) >= MIN_TABLE_ROWS + 1:
            yield run
        run = []
        width = 0
        # la linea que corto puede abrir la siguiente corrida
        if len(cells) >= MIN_TABLE_COLUMNS:
            width = len(cells)
            run = [(start, end, text, cells)]
    if len(run) >= MIN_TABLE_ROWS + 1:
        yield run


def _block(run, delimiter: str, locales) -> TableBlock | None:
    head_start, _, head_text, head_cells = run[0]
    if is_prose_line(head_text, locales):
        return None
    score = header_score(head_cells)
    if score < MIN_HEADER_SCORE:
        return None
    body = run[1:]
    return TableBlock(
        start=head_start, end=body[-1][1], delimiter=delimiter,
        header=tuple(c.strip() for c in head_cells),
        rows=tuple(tuple(c.strip() for c in cells) for _, _, _, cells in body),
        row_spans=tuple((s, e) for s, e, _, _ in body),
        header_score=score,
    )


def find_table_blocks(document: str,
                      locales: tuple[str, ...] | None = None) -> list[TableBlock]:
    """Bloques tabulares embebidos, sin solaparse, el mas grande primero.

    Devuelve CANDIDATOS. Confirmarlos contra el schema es responsabilidad de
    quien llama: la forma sola no distingue un header de una linea de lista.
    """
    if not document or not document.strip():
        return []
    lines = _physical_lines(document)
    if len(lines) < MIN_TABLE_ROWS + 1:
        return []

    candidates: list[TableBlock] = []
    for delimiter in CANDIDATE_DELIMITERS:
        for run in _runs(lines, delimiter):
            if block := _block(run, delimiter, locales):
                candidates.append(block)

    # mas lineas gana; a igualdad, el header mas creible. Asi un bloque que dos
    # delimiters distintos "explican" se resuelve por el que explica mas.
    candidates.sort(key=lambda b: (-b.lines, -b.header_score, b.start))
    chosen: list[TableBlock] = []
    for block in candidates:
        if any(block.start < other.end and other.start < block.end
               for other in chosen):
            continue
        chosen.append(block)
    return sorted(chosen, key=lambda b: b.start)


def blank_spans(document: str, spans) -> str:
    """El documento con esos rangos en blanco, mismo largo y mismos saltos.

    Sirve para que el segmentador de texto libre trabaje sobre lo que queda sin
    que se le muevan los offsets: un bloque tabular ya leido desaparece de su
    vista, pero cada span que produzca sigue apuntando al documento original.
    """
    if not spans:
        return document
    chars = list(document)
    for start, end in spans:
        for i in range(max(0, start), min(len(chars), end)):
            if chars[i] not in "\r\n":
                chars[i] = " "
    return "".join(chars)
