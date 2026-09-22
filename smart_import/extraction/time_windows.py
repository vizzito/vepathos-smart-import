"""Restricciones horarias en lenguaje natural → tw_start / tw_end.

Ejemplos:
  "entregar antes de las 14hs"     → end 14:00 el día de servicio
  "antes de las 4 pm" / "before 4pm"
  "hasta las 16:30"

No usa el modelo: son patrones estables. La fecha de servicio por defecto es
*mañana* (típico en pastes de despacho); se puede pasar ``service_date``.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, time

from ..time_window_range import parse_clock

# antes de las 14hs | before 4 pm | hasta las 16:30 | by 2:00 p.m.
_BEFORE = re.compile(
    r"(?:"
    r"(?:entregar|enviar|mandar|deliver|delivery|entrega)?\s*"
    r"(?:antes\s+de(?:\s+las)?|antes\s+das|before|by|até\s+(?:às|as)|hasta\s+(?:las)?)"
    r")"
    r"\s*"
    r"(?P<h>\d{1,2})"
    r"(?:[:h\.](?P<m>\d{2}))?"
    r"\s*"
    r"(?P<ampm>a\.?\s*m\.?|p\.?\s*m\.?|hs|hrs|h)?"
    r"\b",
    re.IGNORECASE,
)

_NOTE_SPLIT = re.compile(r"\s*\|\s*")


def default_service_date() -> date:
    """'Entregas de mañana' — default operativo del paste de despacho."""
    return date.today() + timedelta(days=1)



def parse_before_constraint(
    text: str,
    service_date: date | None = None,
    timezone: str | None = None,
) -> dict[str, str] | None:
    """Si hay 'antes de las X', devuelve tw_start/tw_end el día de servicio."""
    if not text:
        return None
    match = _BEFORE.search(text)
    if not match:
        return None
    hour = int(match.group("h"))
    minute = int(match.group("m") or 0)
    clock = parse_clock(hour, minute, match.group("ampm"))
    if clock is None:
        return None
    hour, minute = clock
    day = service_date or default_service_date()
    start = datetime.combine(day, time(0, 0))
    end = datetime.combine(day, time(hour, minute))
    if end <= start:
        return None
    from .tz import format_window
    return format_window(start, end, timezone)


def extract_pipe_notes(text: str) -> list[str]:
    """Segmentos después de '|' que no son la dirección principal."""
    parts = _NOTE_SPLIT.split(text or "")
    if len(parts) <= 1:
        return []
    # El primero suele ser name/dir; el resto son notas
    notes = []
    for part in parts[1:]:
        cleaned = re.sub(r"\s+", " ", part).strip(" \t,;")
        if cleaned:
            notes.append(cleaned)
    return notes


def enrich_with_tw_and_reference(
    text: str,
    values: dict[str, str],
    *,
    service_date: date | None = None,
    timezone: str | None = None,
    fields: tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Agrega tw_* y reference derivados del texto original (sin IA)."""
    out = dict(values)
    allowed = set(fields) if fields else None

    tw = parse_before_constraint(text, service_date=service_date, timezone=timezone)
    if tw:
        for key, val in tw.items():
            if allowed is None or key in allowed:
                out.setdefault(key, val)

    notes = extract_pipe_notes(text)
    # Si la nota es solo la restricción TW, igual la dejamos en reference
    # para que el operador la vea; si hay otras (portero), también.
    if notes and (allowed is None or "reference" in allowed):
        ref = "; ".join(notes)
        if ref and not out.get("reference"):
            out["reference"] = ref

    return out
