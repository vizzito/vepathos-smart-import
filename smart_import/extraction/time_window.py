"""Restricciones horarias en lenguaje natural, sin modelo.

Dos cosas distintas que no se mezclan (punto 12 del refactor):

  ``delivery_time_text``  el texto original, siempre, tal cual se escribio
  ``tw_start`` / ``tw_end``  la interpretacion, SOLO si es segura

Si la frase no se entiende, queda el texto y no se inventa la ventana.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time

from .result import FieldValue
from .time_windows import default_service_date, parse_clock
from .tz import format_window

_HOUR = r"(?P<{name}>\d{{1,2}})(?:[:h\.](?P<{name}m>\d{{2}}))?\s*(?P<{name}ap>a\.?\s*m\.?|p\.?\s*m\.?|hs|hrs|h)?"

# "antes de las 14hs" / "hasta las 16:30" / "before 4pm" / "by 2 PM" / "até as 15h"
_BEFORE = re.compile(
    r"(?:antes\s+de(?:\s+las?)?|antes\s+das?|hasta\s+(?:las?\s+)?|before|by|"
    r"at[ée]\s+[àa]s?)\s*" + _HOUR.format(name="e"),
    re.IGNORECASE)
# "despues de las 9" / "a partir de las 10" / "after 9am"
_AFTER = re.compile(
    r"(?:despu[eé]s\s+de(?:\s+las?)?|a\s+partir\s+de(?:\s+las?)?|desde\s+(?:las?\s+)?|"
    r"after|from)\s*" + _HOUR.format(name="s"),
    re.IGNORECASE)
# "entre 9 y 12" / "de 10:00 a 15:00" / "between 9 and 12" / "9 a 12 hs"
_RANGE = re.compile(
    r"(?:entre|de|between|from)\s+" + _HOUR.format(name="s") +
    r"\s*(?:y|a|hasta|and|to|as?)\s+" + _HOUR.format(name="e"),
    re.IGNORECASE)

_PATTERNS = ((_RANGE, "range"), (_BEFORE, "before"), (_AFTER, "after"))


@dataclass(frozen=True)
class TimeHit:
    raw: str
    span: tuple[int, int]
    kind: str
    start: tuple[int, int] | None = None
    end: tuple[int, int] | None = None

    @property
    def understood(self) -> bool:
        return self.start is not None or self.end is not None


def _clock(match: re.Match, prefix: str) -> tuple[int, int] | None:
    """El grupo `prefix` solo existe en los patrones que lo declaran; el kind manda."""
    hour = match.groupdict().get(prefix)
    if hour is None:
        return None
    minute = match.groupdict().get(f"{prefix}m") or 0
    return parse_clock(int(hour), int(minute), match.groupdict().get(f"{prefix}ap"))


def find_time_expression(text: str) -> TimeHit | None:
    """La primera restriccion horaria del texto, con su texto original intacto."""
    from ..time_window_range import RANGE_RE, parse_window
    for match in RANGE_RE.finditer(text or ""):
        raw = match.group().strip()
        # Unadorned '9-11' inside an address is not a delivery window.
        if not re.search(r"[:h]|\b(?:de|entre|between|from|am|pm)\b", raw, re.I):
            continue
        parsed = parse_window(raw)
        if parsed.kind in {"range", "overnight"}:
            return TimeHit(raw, (match.start(), match.start() + len(raw)), "range",
                           parsed.start, parsed.end)
    for pattern, kind in _PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        start = _clock(match, "s") if kind in ("range", "after") else None
        end = _clock(match, "e") if kind in ("range", "before") else None
        if kind == "range" and (start is None or end is None):
            continue
        if start is None and end is None:
            continue
        start_at, end_at = match.span()
        while end_at > start_at and text[end_at - 1] in " .,;:!?)":
            end_at -= 1
        return TimeHit(raw=text[start_at:end_at], span=(start_at, end_at),
                       kind=kind, start=start, end=end)
    return None


def to_window(
    hit: TimeHit,
    service_date: date | None = None,
    timezone: str | None = None,
) -> dict[str, str]:
    """tw_start/tw_end del dia de servicio. Con timezone → UTC. Vacio si no cierra."""
    if hit is None or not hit.understood:
        return {}
    day = service_date or default_service_date()
    start = datetime.combine(day, time(*(hit.start or (0, 0))))
    end = datetime.combine(day, time(*hit.end)) if hit.end else None
    if end is None:
        return {}
    if end <= start:
        return {}
    return format_window(start, end, timezone)


def extract_time(
    canvas,
    context,
    service_date: date | None = None,
    timezone: str | None = None,
) -> list[FieldValue]:
    """El texto crudo siempre; la ventana solo si la interpretacion cierra.

    Mira el ORIGINAL, no el restante: una restriccion horaria vive muchas veces
    adentro de una nota ("Ojo: entregar antes de las 2 PM") y el horario y la nota
    no se excluyen — por eso este paso tampoco consume su span.
    """
    hit = find_time_expression(canvas.original)
    if hit is None:
        return []
    raw = canvas.slice(hit.span)
    out = [FieldValue("delivery_time_text", raw, raw, 0.95, "time_phrase", hit.span,
                      (f"frase horaria '{hit.kind}'",))]
    window = to_window(hit, service_date, timezone)
    for name, value in window.items():
        out.append(FieldValue(name, value, raw, 0.85, "time_parser", hit.span,
                              (f"derivado de '{raw}'",)))
    return out
