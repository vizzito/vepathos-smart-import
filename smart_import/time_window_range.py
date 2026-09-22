"""Bounded, deterministic clock expressions shared by tabular and text imports.

Parsing never supplies a date, an absent endpoint, or a next-day interpretation.
The caller decides those policies. Cells must match completely.
"""
from dataclasses import dataclass
from functools import lru_cache
import re


def clock_pattern(name: str) -> str:
    return (rf"(?P<{name}>\d{{1,2}})(?:[:h.](?P<{name}m>\d{{2}}))?"
            rf"\s*(?P<{name}ap>a\.?\s*m\.?|p\.?\s*m\.?|hrs|hs|h)?")


RANGE_RE = re.compile(
    r"(?<![\w:])(?:(?:de|entre|between|from)\s+)?" + clock_pattern("s") +
    r"\s*(?:[-–—/]|\b(?:a|to|y|and|hasta)\b)\s*" + clock_pattern("e") +
    r"(?![\w:])", re.I)
_BEFORE = re.compile(r"(?:hasta\s+(?:las?\s+)?|antes\s+de\s+(?:las?\s+)?|before\s+|by\s+)" + clock_pattern("e"), re.I)
_AFTER = re.compile(r"(?:despu[eé]s\s+de\s+(?:las?\s+)?|desde\s+(?:las?\s+)?|a\s+partir\s+de\s+(?:las?\s+)?|after\s+)" + clock_pattern("s"), re.I)


def parse_clock(hour: int, minute: int, suffix: str | None = None):
    tag = re.sub(r"[\s.]", "", suffix or "").lower()
    if not 0 <= minute <= 59:
        return None
    if tag in {"am", "pm"}:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if tag == "pm" else 0)
    if not 0 <= hour <= 23:
        return None
    return hour, minute


def _clock(match, prefix):
    return parse_clock(int(match[prefix]), int(match[f"{prefix}m"] or 0), match[f"{prefix}ap"])


@dataclass(frozen=True)
class WindowExpression:
    kind: str
    start: tuple[int, int] | None = None
    end: tuple[int, int] | None = None


def parse_window(text: str) -> WindowExpression:
    text = text.strip()
    if not text:
        return WindowExpression("empty")
    if len(text) > 256:
        return WindowExpression("invalid")
    return _parse_window(text)


@lru_cache(maxsize=2048)
def _parse_window(text: str) -> WindowExpression:
    match = RANGE_RE.fullmatch(text)
    if match:
        start, end = _clock(match, "s"), _clock(match, "e")
        if start is None or end is None or start == end:
            return WindowExpression("invalid")
        return WindowExpression("overnight" if end < start else "range", start, end)
    for pattern, kind, prefix in ((_BEFORE, "before", "e"), (_AFTER, "after", "s")):
        match = pattern.fullmatch(text)
        if match:
            clock = _clock(match, prefix)
            if clock is not None:
                return WindowExpression(kind, clock if prefix == "s" else None,
                                        clock if prefix == "e" else None)
    return WindowExpression("invalid")
