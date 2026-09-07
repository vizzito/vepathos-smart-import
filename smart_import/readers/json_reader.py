"""JSON: aplana addresses[].packages[] a filas.

Se aplana usando el NOMBRE HOJA de cada clave (`dimensions.length` -> `length`,
`time_window.start` -> `start`), y solo si hay colision se usa el path completo.
Asi el JSON pasa por el MISMO mapper de aliases que un CSV, sin casos especiales.

Resiliencia: un literal roto (p.ej. `"lng": -71.`) o una address corrupta NO
tumba el import entero. Se repara lo tipico, se salvan los objetos parseables
y se anota lo descartado en `meta.notes`. Lat/lng fuera de rango o invertidas
siguen siendo responsabilidad del RowNormalizer (rejected_coordinates / warnings).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .base import FileMeta, Table, dedupe_columns

LIST_KEYS = ("addresses", "deliveries", "stops", "waypoints", "items", "rows", "data", "orders")
CHILD_KEYS = ("packages", "bultos", "paquetes", "items")

#: `-71.` / `42.` no son JSON; el export a veces trunca el decimal.
_TRAILING_DOT = re.compile(r"(?<![.\w])(-?\d+)\.(?=\s*[,}\]])")
#: `.521469` tampoco (falta el 0 a la izquierda)
_LEADING_DOT = re.compile(r"(?<![.\d\w])(-?)\.(\d+)(?=\s*[,}\]])")
#: trailing commas antes de } o ]
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
#: NaN / Infinity (JSON estricto no los acepta)
_NONFINITE = re.compile(r"\b(-?Infinity|NaN)\b")
#: literales numericos basura en coords: `42.oppeep`, `-pepe.152915`
#: (no tocar true/false/null ni otros valores)
_BAD_NUMBER = re.compile(
    r'("(?:lat|lng|lon|longitude|latitude)"\s*:\s*)('
    r"-?\d+\.[A-Za-z_]\w*"          # 42.oppeep
    r"|-?[A-Za-z_]\w*\.\d+"         # -pepe.152915
    r"|-?[A-Za-z_]\w+"              # pepe
    r")\b"
)
#: `dimensions` sin cerrar: `"height": 7.6` pegado a `"packaging"` / siblings
_MISSING_DIM_CLOSE = re.compile(
    r'("(?:length|width|height)"\s*:\s*-?\d+(?:\.\d+)?)\s*'
    r'(\n\s*)"(packaging|status|time_window|value_cents|value_currency|weight_kg)"'
)
#: inicio tipico de un address (evita enganchar `{cester` / `{ ambridge`)
_ADDRESS_START = re.compile(
    r'\{\s*"(?:lat|lng|address|delivery_id|packages|bultos|paquetes|zone|customer_name)"'
)
#: tope de tamaño de un address individual al salvar (evita parsear MB engullidos)
_MAX_ADDRESS_CHARS = 100_000
#: claves tipicas de un address (no de un package suelto)
_ADDRESS_KEYS = frozenset({
    "packages", "bultos", "paquetes", "lat", "lng", "address",
    "delivery_id", "zone", "customer_name", "phone",
})


def _find_records(doc: Any) -> tuple[list[dict], str | None]:
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, dict)], None
    if isinstance(doc, dict):
        for key in LIST_KEYS:
            val = doc.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val, key
        for key, val in doc.items():                     # cualquier lista de objetos
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val, key
        return [doc], None
    return [], None


def _flatten(obj: dict, prefix: str = "") -> dict[str, Any]:
    """Aplana escalares; devuelve {path_completo: valor}. Ignora listas de objetos."""
    out: dict[str, Any] = {}
    for key, val in obj.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(val, dict):
            out.update(_flatten(val, path))
        elif isinstance(val, list):
            if val and all(not isinstance(v, (dict, list)) for v in val):
                out[path] = ", ".join(str(v) for v in val)
        elif val is not None:
            # null (p.ej. time_window: null) no genera columna basura
            out[path] = val
    return out


def _leaf_names(paths: list[str]) -> dict[str, str]:
    """path -> nombre de columna (hoja si es unica, path completo si colisiona)."""
    counts: dict[str, int] = {}
    for p in paths:
        counts[p.rsplit(".", 1)[-1]] = counts.get(p.rsplit(".", 1)[-1], 0) + 1
    return {p: (leaf if counts[leaf] == 1 else p) for p in paths
            for leaf in [p.rsplit(".", 1)[-1]]}


def read(path: str | Path, max_rows: int | None = None) -> Table:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    doc, load_notes = loads_resilient(raw)

    records, container = _find_records(doc)

    expanded: list[dict[str, Any]] = []
    child_key_used: str | None = None
    synthesized_ids = 0
    for index, rec in enumerate(records, start=1):
        if not isinstance(rec, dict):
            continue
        child: list[dict] = []
        for key in CHILD_KEYS:
            val = rec.get(key)
            if isinstance(val, list) and any(isinstance(v, dict) for v in val):
                child = [v for v in val if isinstance(v, dict)]
                child_key_used = key
                break

        # El objeto padre define la entrega. Sin delivery_id, los N packages
        # quedarian como N stops (group_key=row). Sintetizamos uno estable.
        parent_raw = {k: v for k, v in rec.items() if k != child_key_used}
        if _blank(parent_raw.get("delivery_id")) and _blank(parent_raw.get("id")):
            parent_raw["delivery_id"] = f"DLV-{index:05d}"
            synthesized_ids += 1

        parent = _flatten(parent_raw)
        if not child:
            expanded.append(parent)
        else:
            for item in child:
                row = dict(parent)
                row.update(_flatten(item))
                expanded.append(row)
        if max_rows and len(expanded) >= max_rows:
            break

    paths: list[str] = []
    for row in expanded:
        for k in row:
            if k not in paths:
                paths.append(k)
    rename = _leaf_names(paths)
    columns = dedupe_columns([rename[p] for p in paths])

    rows = [tuple(row.get(p) for p in paths) for row in expanded]

    meta = FileMeta(path=str(p), format="json", size_bytes=p.stat().st_size)
    meta.notes.extend(load_notes)
    if container:
        meta.notes.append(f"registros tomados de '{container}'")
    if child_key_used:
        meta.notes.append(f"expandido por '{child_key_used}': 1 fila por delivery x package")
    if synthesized_ids:
        meta.notes.append(
            f"delivery_id sintetico en {synthesized_ids} address(es) sin id "
            f"(DLV-00001…); agrupa packages del mismo objeto padre"
        )
    return Table(meta=meta, columns=columns, rows=rows)


def loads_resilient(text: str) -> tuple[Any, list[str]]:
    """Parsea JSON tolerando roturas tipicas de export; anota reparaciones.

    Orden:
      1. parse estricto
      2. reparar literales rotos (``.`` trailing, trailing commas, NaN) y reintentar
      3. salvar objetos de la lista principal uno a uno; descartar los irrecuperables
    """
    notes: list[str] = []
    try:
        return json.loads(text), notes
    except json.JSONDecodeError as first:
        first_err = first

    repaired, fixes = repair_json_text(text)
    if fixes:
        notes.append(
            "JSON reparado antes de parsear: " + "; ".join(fixes)
        )
        try:
            return json.loads(repaired), notes
        except json.JSONDecodeError:
            pass
    else:
        repaired = text

    salvaged, salvage_notes = salvage_list_document(repaired)
    notes.extend(salvage_notes)
    if salvaged is not None:
        return salvaged, notes

    raise first_err


def repair_json_text(text: str) -> tuple[str, list[str]]:
    """Arreglos locales que no cambian la semantica de un float bien formado."""
    fixes: list[str] = []
    out = text

    n_dot = len(_TRAILING_DOT.findall(out))
    if n_dot:
        out = _TRAILING_DOT.sub(r"\1.0", out)
        fixes.append(f"{n_dot} numero(s) con decimal truncado (p.ej. -71. → -71.0)")

    n_lead = len(_LEADING_DOT.findall(out))
    if n_lead:
        out = _LEADING_DOT.sub(r"\g<1>0.\2", out)
        fixes.append(f"{n_lead} numero(s) con punto inicial (p.ej. .52 → 0.52)")

    n_bad = len(_BAD_NUMBER.findall(out))
    if n_bad:
        out = _BAD_NUMBER.sub(r"\1null", out)
        fixes.append(f"{n_bad} literal(es) numerico(s) invalido(s) → null (p.ej. 42.oppeep)")

    n_dim = len(_MISSING_DIM_CLOSE.findall(out))
    if n_dim:
        out = _MISSING_DIM_CLOSE.sub(r'\1\2},\2"\3"', out)
        fixes.append(f"{n_dim} dimensions sin cerrar (se inserto '}}' antes del sibling)")

    n_comma = 0
    while True:
        new, count = _TRAILING_COMMA.subn(r"\1", out)
        if count == 0:
            break
        n_comma += count
        out = new
    if n_comma:
        fixes.append(f"{n_comma} trailing comma(s) eliminada(s)")

    n_nf = len(_NONFINITE.findall(out))
    if n_nf:
        out = _NONFINITE.sub("null", out)
        fixes.append(f"{n_nf} NaN/Infinity → null")

    return out, fixes


def salvage_list_document(text: str) -> tuple[Any | None, list[str]]:
    """Si el doc entero no parsea, intenta recuperar cada objeto de addresses[]."""
    notes: list[str] = []
    key, start = _find_list_start(text)
    if start < 0:
        return None, notes

    kept, scan_notes = _salvage_addresses(text, start)
    notes.extend(scan_notes)
    if not kept:
        return None, notes

    notes.append(
        f"salvamento: {len(kept)} address(es) recuperada(s) desde '{key}'"
    )

    wrapper_prefix = text[: _find_key_span(text, key)[0]].rstrip()
    doc: dict[str, Any] = {key: kept}
    if wrapper_prefix.startswith("{"):
        stub = wrapper_prefix + "}"
        stub_repaired, _ = repair_json_text(stub)
        try:
            meta = json.loads(stub_repaired)
            if isinstance(meta, dict):
                meta[key] = kept
                return meta, notes
        except json.JSONDecodeError:
            pass
    return doc, notes


def _find_list_start(text: str) -> tuple[str, int]:
    """Devuelve (clave, indice del '[' de la lista de records)."""
    for key in LIST_KEYS:
        m = re.search(rf'"{re.escape(key)}"\s*:', text)
        if not m:
            continue
        i = m.end()
        while i < len(text) and text[i].isspace():
            i += 1
        if i < len(text) and text[i] == "[":
            return key, i
    # primera lista de objetos
    m = re.search(r'"([^"]+)"\s*:\s*\[', text)
    if m:
        return m.group(1), m.end() - 1
    return "", -1


def _find_key_span(text: str, key: str) -> tuple[int, int]:
    m = re.search(rf'"{re.escape(key)}"\s*:', text)
    if not m:
        return 0, 0
    return m.start(), m.end()


def _salvage_addresses(text: str, array_start: int) -> tuple[list[dict], list[str]]:
    """Recupera addresses buscando `{` + clave tipica.

    Critico: solo avanza `used_until` si el chunk PARSEA. Si un address tiene
    comillas/braces rotos, el matching de `}` puede "engullir" miles de vecinos
    sanos — en ese caso avanzamos solo 1 char y seguimos buscando.
    """
    notes: list[str] = []
    kept: list[dict] = []
    used_until = array_start
    skipped = 0
    discarded = 0
    for match in _ADDRESS_START.finditer(text, array_start):
        start = match.start()
        if start < used_until:
            continue
        end = _matching_brace(text, start)
        if end < 0 or (end - start) > _MAX_ADDRESS_CHARS:
            skipped += 1
            used_until = start + 1
            continue
        chunk = text[start: end + 1]
        obj = _parse_object_chunk(chunk)
        if obj is not None and _looks_like_address(obj):
            kept.append(obj)
            used_until = end + 1
            continue
        # span sospechoso: no consumirlo entero
        discarded += 1
        if discarded <= 5:
            preview = re.sub(r"\s+", " ", chunk.strip())[:80]
            notes.append(f"address descartada (JSON irrecuperable): {preview}…")
        used_until = start + 1
    if discarded > 5:
        notes.append(f"… y {discarded - 5} address(es) irrecuperable(s) mas")
    if skipped:
        notes.append(
            f"{skipped} candidato(s) de address sin cerrar omitido(s) "
            f"(truncado o braces rotos)"
        )
    garbage = 0
    scan_end = used_until if used_until > array_start else len(text)
    for _ in re.finditer(r'\{\s*[^"\s\{]', text[array_start:scan_end]):
        garbage += 1
    if garbage:
        notes.append(f"{garbage} fragmento(s) basura entre addresses ignorado(s)")
    return kept, notes


def _split_top_level_objects(text: str, array_start: int) -> tuple[list[str], list[str]]:
    """Compat: delega en _salvage_addresses y devuelve chunks serializados."""
    kept, notes = _salvage_addresses(text, array_start)
    return [json.dumps(obj, ensure_ascii=False) for obj in kept], notes


def _looks_like_address(obj: dict) -> bool:
    """Filtra packages sueltos que el scanner pueda haber capturado al saltar basura."""
    return bool(_ADDRESS_KEYS & obj.keys())


def _matching_brace(text: str, start: int) -> int:
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _parse_object_chunk(chunk: str) -> dict | None:
    try:
        obj = json.loads(chunk)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    repaired, _ = repair_json_text(chunk)
    try:
        obj = json.loads(repaired)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())
