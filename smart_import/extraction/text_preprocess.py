"""Preproceso de texto libre (solo documento free_text).

Transformaciones *aditivas* y acotadas: no reescriben WhatsApp/CSV normales.

  1. Lineas `a=b&c=d` (forms / webhooks) → `a: b | c: d` legible
  2. Numeros hablados en espanol → digitos (tel / alturas)

Se aplica UNA vez al documento antes de segmentar. El path tabular no pasa por aca.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote_plus

# --- query-string / form encoding -------------------------------------------

_QS_LINE = re.compile(
    r"^[A-Za-z_][\w]*=[^\s]+(?:&[A-Za-z_][\w]*=[^\s]*)+$"
)


def rewrite_querystring_line(line: str) -> str | None:
    """Si la linea es query-string, la reescribe; si no, None."""
    raw = (line or "").strip()
    if not raw or not _QS_LINE.match(raw):
        return None
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return None
    if len(pairs) < 2:
        return None
    parts: list[str] = []
    for key, value in pairs:
        value = unquote_plus(value or "").replace("_", " ").strip()
        key = key.strip()
        if not key:
            continue
        parts.append(f"{key}: {value}" if value else f"{key}:")
    return " | ".join(parts) if parts else None


# --- numeros hablados (es) --------------------------------------------------

def _fold(s: str) -> str:
    return (
        s.lower()
        .replace("á", "a").replace("é", "e").replace("í", "i")
        .replace("ó", "o").replace("ú", "u").replace("ü", "u")
    )


_UNITS = {
    "cero": "0", "oh": "0",
    "un": "1", "uno": "1", "una": "1",
    "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5",
    "seis": "6", "siete": "7", "ocho": "8", "nueve": "9",
}
_TEENS = {
    "diez": "10", "once": "11", "doce": "12", "trece": "13",
    "catorce": "14", "quince": "15",
    "dieciseis": "16", "diecisiete": "17", "dieciocho": "18", "diecinueve": "19",
}
_TENS = {
    "veinte": "20", "treinta": "30", "cuarenta": "40", "cincuenta": "50",
    "sesenta": "60", "setenta": "70", "ochenta": "80", "noventa": "90",
}
_VEINTI = {
    "veintiun": "21", "veintiuno": "21", "veintiuna": "21",
    "veintidos": "22", "veintitres": "23", "veinticuatro": "24",
    "veinticinco": "25", "veintiseis": "26", "veintisiete": "27",
    "veintiocho": "28", "veintinueve": "29",
}
_HUNDREDS = {
    "cien": 100, "ciento": 100,
    "doscientos": 200, "trescientos": 300, "cuatrocientos": 400,
    "quinientos": 500, "seiscientos": 600, "setecientos": 700,
    "ochocientos": 800, "novecientos": 900,
}
_UNITS_INT = {k: int(v) for k, v in _UNITS.items()}
_TEENS_INT = {k: int(v) for k, v in _TEENS.items()}
_TENS_INT = {k: int(v) for k, v in _TENS.items()}
_VEINTI_INT = {k: int(v) for k, v in _VEINTI.items()}

# palabras que disparan el expander (evita tocar texto sin numeros hablados)
_ANY_NUM = re.compile(
    r"\b(?:cero|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|"
    r"diez|once|doce|trece|catorce|quince|dieci\w+|veinti\w+|veinte|"
    r"treinta|cuarenta|cincuenta|sesenta|setenta|ochenta|noventa|"
    r"cien|ciento|doscientos|trescientos|cuatrocientos|quinientos|"
    r"seiscientos|setecientos|ochocientos|novecientos|mil)\b",
    re.IGNORECASE,
)

# "cuatro mil trescientos" / "dos mil quinientos"
_MIL = re.compile(
    r"\b(?P<a>un|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|"
    r"once|doce|trece|catorce|quince)?\s*mil"
    r"(?:\s+(?P<h>ciento|doscientos|trescientos|cuatrocientos|quinientos|"
    r"seiscientos|setecientos|ochocientos|novecientos))?"
    r"(?:\s+(?P<t>veinte|treinta|cuarenta|cincuenta|sesenta|setenta|ochenta|noventa))?"
    r"(?:\s+y?\s*(?P<u>uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve))?"
    r"\b",
    re.IGNORECASE,
)

# "cincuenta y cuatro" / "treinta y tres"
_TENS_UNITS = re.compile(
    r"\b(?P<t>veinte|treinta|cuarenta|cincuenta|sesenta|setenta|ochenta|noventa)"
    r"(?:\s+y\s+(?P<u>uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve))\b",
    re.IGNORECASE,
)


def _mil_repl(m: re.Match) -> str:
    a = _fold(m.group("a") or "un")
    thousands = _UNITS_INT.get(a) or _TEENS_INT.get(a) or 1
    total = thousands * 1000
    if m.group("h"):
        total += _HUNDREDS[_fold(m.group("h"))]
    if m.group("t"):
        total += _TENS_INT[_fold(m.group("t"))]
    if m.group("u"):
        total += _UNITS_INT[_fold(m.group("u"))]
    return str(total)


def _tens_repl(m: re.Match) -> str:
    total = _TENS_INT[_fold(m.group("t"))]
    if m.group("u"):
        total += _UNITS_INT[_fold(m.group("u"))]
    return str(total)


def expand_spoken_es_numbers(text: str) -> str:
    """Reemplaza numeros hablados ES por digitos, en pasadas seguras."""
    if not text or not _ANY_NUM.search(text):
        return text

    out = text
    # No convertir "punto uno/dos" (whisper/IVR: 'nota de voz punto uno')
    protected: list[str] = []

    def _protect(m: re.Match) -> str:
        protected.append(m.group(0))
        return f" __P{len(protected) - 1}__ "

    out = re.sub(
        r"\b(?:punto|puntos)\s+"
        r"(?:uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)\b",
        _protect,
        out,
        flags=re.IGNORECASE,
    )

    out = _MIL.sub(_mil_repl, out)
    out = _TENS_UNITS.sub(_tens_repl, out)

    def repl_word(mapping: dict[str, str | int]) -> None:
        nonlocal out
        for word, val in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
            out = re.sub(
                rf"\b{re.escape(word)}\b",
                str(val),
                out,
                flags=re.IGNORECASE,
            )

    repl_word(_VEINTI)
    repl_word(_TEENS)
    repl_word(_TENS)
    for word, val in _HUNDREDS.items():
        out = re.sub(rf"\b{re.escape(word)}\b", str(val), out, flags=re.IGNORECASE)
    repl_word(_UNITS)

    def collapse_phoneish(m: re.Match) -> str:
        parts = m.group(0).split()
        digits = "".join(parts)
        if len(digits) >= 6:
            return digits
        return m.group(0)

    out = re.sub(
        r"(?<!\d)(?:\d{1,2})(?:\s+\d{1,2}){3,}(?!\d)",
        collapse_phoneish,
        out,
    )

    for i, raw in enumerate(protected):
        out = out.replace(f"__P{i}__", raw)
        out = out.replace(f" __P{i}__ ", f" {raw} ")
    return out


def preprocess_free_text_document(document: str) -> tuple[str, list[str]]:
    """Aplica rewrites de linea (QS) + expansion hablada. Devuelve (texto, notas)."""
    notes: list[str] = []
    if not document:
        return document, notes

    lines = document.splitlines(keepends=True)
    new_lines: list[str] = []
    qs = 0
    for line in lines:
        ending = ""
        core = line
        if line.endswith("\r\n"):
            ending = "\r\n"
            core = line[:-2]
        elif line.endswith("\n"):
            ending = "\n"
            core = line[:-1]
        rewritten = rewrite_querystring_line(core)
        if rewritten is not None:
            new_lines.append(rewritten + ending)
            qs += 1
        else:
            new_lines.append(line)
    text = "".join(new_lines)
    if qs:
        notes.append(f"preproceso: {qs} linea(s) query-string reescritas a key: value")

    expanded = expand_spoken_es_numbers(text)
    if expanded != text:
        notes.append("preproceso: numeros hablados (es) convertidos a digitos")
        text = expanded
    return text, notes
