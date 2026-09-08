"""Corta un documento de texto libre en posibles entregas.

Regla central: **una linea no es una entrega**. Un paste real trae preambulo,
entregas que pueden ocupar mas de una linea, y una despedida. El segmentador solo
decide *donde cortar* y guarda los offsets; quien decide si un segmento ES una
entrega es `DeliveryCandidateClassifier`.

El texto original nunca se modifica: cada `Segment` guarda el span exacto y el
texto tal cual vino.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .text_mode import _BULLET

#: viñetas minimas para creer que la lista esta viñetada
MIN_BULLETS = 3
#: bloques minimos separados por linea en blanco
MIN_BLOCKS = 3
#: separador de campos que tambien puede separar registros si no hay nada mejor
_SLASHES = re.compile(r"\s*//\s*")
#: una linea "se sostiene sola" si es larga y trae algun numero (altura, tel, CP).
#: Sirve para distinguir 12 entregas pegadas de una direccion escrita en 3 renglones.
MIN_SELF_CONTAINED_CHARS = 25
_HAS_DIGIT = re.compile(r"\d")

PREAMBLE = "preamble"
RECORD = "record"
FOOTER = "footer"


@dataclass(frozen=True)
class Segment:
    """Un trozo del documento con su ubicacion exacta."""
    text: str
    start: int
    end: int
    kind: str = RECORD
    #: numero de la lista si la linea venia como '1)' / '3.' — util como delivery_id
    list_number: str | None = None

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)

    @property
    def body(self) -> str:
        """El contenido sin la marca de lista. `text` sigue siendo el original."""
        stripped = _BULLET_PREFIX.sub("", self.text)
        if stripped != self.text:
            return stripped
        if self.list_number and _BARE_NEXT.match(self.text):
            return _BARE_NEXT.sub("", self.text, count=1)
        return self.text

    def as_dict(self) -> dict:
        return {"text": self.text, "span": [self.start, self.end], "kind": self.kind,
                "list_number": self.list_number}


@dataclass
class SegmentedDocument:
    document: str
    strategy: str = "line"
    segments: list[Segment] = field(default_factory=list)
    preamble: list[Segment] = field(default_factory=list)
    footer: list[Segment] = field(default_factory=list)

    @property
    def all_segments(self) -> list[Segment]:
        return sorted([*self.preamble, *self.segments, *self.footer],
                      key=lambda s: s.start)

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "segments": len(self.segments),
            "preamble": [s.text for s in self.preamble],
            "footer": [s.text for s in self.footer],
        }


_LIST_NUMBER = re.compile(r"^\s*(?:[-*•·–—]\s*)?(\d{1,3})[)\.\-:]\s+")
_BULLET_PREFIX = re.compile(r"^\s*(?:\d{1,3}[)\.\-:]|[-*•·–—])\s+")
#: item siguiente sin puntuacion: '6 nombre' (no '6.' / '6)'). Solo si N == ultimo+1.
_BARE_NEXT = re.compile(r"^\s*(\d{1,3})\s+(?=[^\W\d_])", re.UNICODE)


def _list_number(text: str) -> str | None:
    match = _LIST_NUMBER.match(text)
    return match.group(1) if match else None


def _last_list_number(document: str, groups: list[tuple[int, int]]) -> int | None:
    """Numero del ultimo item de la lista (5. / 5) / 6 nombre)."""
    if not groups:
        return None
    start, end = groups[-1]
    first = document[start:end].splitlines()[0]
    raw = _list_number(first)
    if raw is None:
        bare = _BARE_NEXT.match(first)
        raw = bare.group(1) if bare else None
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _physical_lines(document: str) -> list[tuple[int, int, str]]:
    """(start, end, texto) de cada linea, con offsets absolutos en el documento."""
    out: list[tuple[int, int, str]] = []
    offset = 0
    for raw in document.splitlines(keepends=True):
        stripped = raw.rstrip("\r\n")
        out.append((offset, offset + len(stripped), stripped))
        offset += len(raw)
    return out


def _segment(document: str, start: int, end: int, kind: str,
             list_number: str | None = None) -> Segment:
    text = document[start:end]
    lead = len(text) - len(text.lstrip())
    trail = len(text) - len(text.rstrip())
    start += lead
    end -= trail
    body = document[start:end]
    if list_number is None:
        list_number = _list_number(body)
    return Segment(text=body, start=start, end=end, kind=kind,
                   list_number=list_number)


class FreeTextSegmenter:
    """Elige la estrategia de corte segun la forma del documento."""

    def __init__(self, min_bullets: int = MIN_BULLETS, min_blocks: int = MIN_BLOCKS):
        self.min_bullets = min_bullets
        self.min_blocks = min_blocks

    def split(self, document: str) -> SegmentedDocument:
        lines = _physical_lines(document)
        filled = [(s, e, t) for s, e, t in lines if t.strip()]
        if not filled:
            return SegmentedDocument(document=document, strategy="empty")

        bullets = [i for i, (_, _, t) in enumerate(lines) if _BULLET.match(t)]
        if len(bullets) >= self.min_bullets:
            return self._by_bullets(document, lines, set(bullets))

        blocks = self._blocks(lines)
        if len(blocks) >= self.min_blocks:
            return self._by_blocks(document, blocks)

        if len(filled) == 1 and len(_SLASHES.split(filled[0][2])) >= self.min_blocks:
            return self._by_slashes(document, filled[0])

        return self._by_lines(document, filled)

    # ---------- estrategias ----------

    def _by_bullets(self, document: str, lines, bullets: set[int]) -> SegmentedDocument:
        """Viñetas: cada una abre un registro; lo que sigue sin viñeta lo continua."""
        out = SegmentedDocument(document=document, strategy="bullet")
        first, last = min(bullets), max(bullets)
        groups: list[tuple[int, int]] = []          # (start, end) por registro

        for i, (start, end, text) in enumerate(lines):
            if i < first:
                if text.strip():
                    out.preamble.append(_segment(document, start, end, PREAMBLE))
                continue
            if i in bullets:
                groups.append((start, end))
                continue
            if not text.strip():
                continue
            # '6 martin…' despues de '5.' es el item 6, no una nota de Lucia.
            prev = _last_list_number(document, groups)
            nxt = _BARE_NEXT.match(text)
            if nxt and prev is not None and int(nxt.group(1)) == prev + 1:
                groups.append((start, end))
                continue
            if i > last and self._closes(i, lines, last):
                out.footer.append(_segment(document, start, end, FOOTER))
                continue
            if groups:                              # continuacion del registro abierto
                groups[-1] = (groups[-1][0], end)

        out.segments = []
        for s, e in groups:
            first = document[s:e].splitlines()[0]
            n = _list_number(first)
            if n is None:
                bare = _BARE_NEXT.match(first)
                n = bare.group(1) if bare else None
            out.segments.append(_segment(document, s, e, RECORD, list_number=n))
        return out

    @staticmethod
    def _closes(index: int, lines, last_bullet: int) -> bool:
        """Despues de la ultima viñeta, una linea en blanco corta el bloque: lo que
        viene despues es despedida, no continuacion."""
        return any(not lines[j][2].strip() for j in range(last_bullet + 1, index))

    def _blocks(self, lines) -> list[tuple[int, int]]:
        """Bloques separados por al menos una linea en blanco."""
        blocks: list[tuple[int, int]] = []
        open_block: tuple[int, int] | None = None
        for start, end, text in lines:
            if text.strip():
                open_block = (open_block[0], end) if open_block else (start, end)
            elif open_block:
                blocks.append(open_block)
                open_block = None
        if open_block:
            blocks.append(open_block)
        return blocks

    def _by_blocks(self, document: str, blocks) -> SegmentedDocument:
        """Un bloque = un registro, salvo que sus lineas se sostengan solas.

        Doce entregas pegadas entre dos lineas en blanco son un solo bloque pero
        doce registros. Una direccion escrita en tres renglones cortos es un solo
        registro. La diferencia es si cada linea tiene cuerpo y algun numero.
        """
        out = SegmentedDocument(document=document, strategy="block")
        for start, end in blocks:
            for piece in self._explode(document, start, end):
                out.segments.append(piece)
        return out

    def _explode(self, document: str, start: int, end: int) -> list[Segment]:
        inner = [(s, e, t) for s, e, t in _physical_lines(document[start:end]) if t.strip()]
        if len(inner) < 2:
            return [_segment(document, start, end, RECORD)]
        standalone = sum(1 for _, _, t in inner
                         if len(t.strip()) >= MIN_SELF_CONTAINED_CHARS and _HAS_DIGIT.search(t))
        if standalone < len(inner) / 2:
            return [_segment(document, start, end, RECORD)]
        return [_segment(document, start + s, start + e, RECORD) for s, e, _ in inner]

    def _by_slashes(self, document: str, line) -> SegmentedDocument:
        """Documento de una sola linea con '//': cada trozo es un registro."""
        start, _, text = line
        out = SegmentedDocument(document=document, strategy="slashes")
        offset = start
        for part in _SLASHES.split(text):
            if part.strip():
                begin = document.index(part, offset)
                out.segments.append(_segment(document, begin, begin + len(part), RECORD))
                offset = begin + len(part)
        return out

    def _by_lines(self, document: str, filled) -> SegmentedDocument:
        """Sin separador claro: cada linea es un candidato y el clasificador filtra."""
        out = SegmentedDocument(document=document, strategy="line")
        out.segments = [_segment(document, s, e, RECORD) for s, e, _ in filled]
        return out
