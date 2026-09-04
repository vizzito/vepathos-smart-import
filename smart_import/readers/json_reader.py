"""JSON: aplana addresses[].packages[] a filas.

Se aplana usando el NOMBRE HOJA de cada clave (`dimensions.length` -> `length`,
`time_window.start` -> `start`), y solo si hay colision se usa el path completo.
Asi el JSON pasa por el MISMO mapper de aliases que un CSV, sin casos especiales.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import FileMeta, Table, dedupe_columns

LIST_KEYS = ("addresses", "deliveries", "stops", "waypoints", "items", "rows", "data")
CHILD_KEYS = ("packages", "bultos", "paquetes", "items")


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
        else:
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
    doc = json.loads(p.read_text(encoding="utf-8"))
    records, container = _find_records(doc)

    expanded: list[dict[str, Any]] = []
    child_key_used: str | None = None
    for rec in records:
        child: list[dict] = []
        for key in CHILD_KEYS:
            val = rec.get(key)
            if isinstance(val, list) and any(isinstance(v, dict) for v in val):
                child = [v for v in val if isinstance(v, dict)]
                child_key_used = key
                break
        parent = _flatten({k: v for k, v in rec.items() if k != child_key_used})
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
    if container:
        meta.notes.append(f"registros tomados de '{container}'")
    if child_key_used:
        meta.notes.append(f"expandido por '{child_key_used}': 1 fila por delivery x package")
    return Table(meta=meta, columns=columns, rows=rows)
