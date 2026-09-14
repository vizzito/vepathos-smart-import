"""Escritura del texto (CJK vs latino) sin forks por país.

Identificar kanji/kana no geocodifica Japón por sí solo: el índice tiene que
tener esos tokens. Sirve para no tratar un ward (大田区) como ruido latino y
para incluirlo en la búsqueda FTS.
"""
from __future__ import annotations

import re

_SPLIT = re.compile(r"[\s,;|/]+")

# Hiragana, katakana, CJK unificado, compatibilidad, halfwidth kana.
_CJK_RANGES = (
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFF66, 0xFF9D),
)


def has_cjk(text: str) -> bool:
    """True si hay al menos un carácter han / kana."""
    for ch in text or "":
        code = ord(ch)
        for start, end in _CJK_RANGES:
            if start <= code <= end:
                return True
    return False


def cjk_tokens(text: str) -> list[str]:
    """Segmentos (coma/espacio) que contienen CJK: chome, ward, municipio."""
    out: list[str] = []
    seen: set[str] = set()
    for part in _SPLIT.split(text or ""):
        token = part.strip(" .,;:-")
        if not token or not has_cjk(token):
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out
