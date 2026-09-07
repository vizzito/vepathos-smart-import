"""Texto original inmutable + mascara de lo ya consumido.

Cada extractor achica la ambiguedad para el siguiente: cuando el telefono ya se
identifico, el que busca el nombre no tiene que pelear con esos digitos. Pero el
original NUNCA se destruye: los spans siguen siendo validos contra el texto de
entrada, asi que todo valor extraido se puede rastrear hasta su posicion exacta.
"""
from __future__ import annotations


class TextCanvas:
    """`original` no cambia; `remaining()` devuelve el texto con lo consumido en blanco."""

    __slots__ = ("original", "_mask")

    def __init__(self, text: str):
        self.original = text or ""
        self._mask = [False] * len(self.original)

    def consume(self, span: tuple[int, int] | None) -> None:
        start, end = span if span else (0, 0)
        for i in range(max(0, start), min(len(self._mask), end)):
            self._mask[i] = True

    def is_free(self, span: tuple[int, int]) -> bool:
        start, end = span
        return not any(self._mask[max(0, start):min(len(self._mask), end)])

    def remaining(self) -> str:
        """Mismo largo que el original: los offsets siguen sirviendo."""
        return "".join(" " if used else ch
                       for ch, used in zip(self.original, self._mask))

    def remaining_text(self) -> str:
        """Lo que queda, colapsado — para leerlo, no para calcular offsets."""
        return " ".join(self.remaining().split())

    def slice(self, span: tuple[int, int]) -> str:
        return self.original[span[0]:span[1]]

    def find(self, needle: str, start: int = 0) -> tuple[int, int] | None:
        if not needle:
            return None
        position = self.original.find(needle, start)
        if position < 0:
            return None
        return (position, position + len(needle))

    def __len__(self) -> int:
        return len(self.original)
