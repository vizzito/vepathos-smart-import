"""Filas planas -> deliveries con packages.

Dos formas de venir el mismo dato:
  A) una fila POR BULTO, repitiendo los datos de la entrega  -> agrupar
  B) una fila POR ENTREGA con 'bultos: 3'                    -> expandir
Ambas terminan en la misma estructura.
"""
from __future__ import annotations

from typing import Any

from ..normalization.row_normalizer import NormalizedRow
from ..normalization.values import is_blank
from ..schemas import TargetSchema

DIMENSION_FIELDS = {"length_cm": "length", "width_cm": "width", "height_cm": "height"}
TW_FIELDS = {"tw_start": "start", "tw_end": "end", "tw_timezone": "time_zone"}


def _package_payload(row: NormalizedRow, schema: TargetSchema) -> dict[str, Any]:
    pkg: dict[str, Any] = {}
    dims: dict[str, Any] = {}
    for name in schema.column_order:
        if name not in row.values or schema.fields[name].level != "package":
            continue
        value = row.values.get(name)
        if is_blank(value):
            continue
        if name in DIMENSION_FIELDS:
            dims[DIMENSION_FIELDS[name]] = value
        else:
            pkg[name] = value
    if dims:
        pkg["dimensions"] = dims
    return pkg


def _time_window(row: NormalizedRow) -> dict[str, Any] | None:
    tw = {out: row.values.get(src) for src, out in TW_FIELDS.items()
          if not is_blank(row.values.get(src))}
    return tw if tw.get("start") and tw.get("end") else None


def has_package(row: NormalizedRow, schema: TargetSchema) -> bool:
    return any(
        not is_blank(row.values.get(n))
        for n in row.values
        if n in schema.fields and schema.fields[n].level == "package"
    )


def group_key(row: NormalizedRow, schema: TargetSchema) -> str:
    """delivery_id si existe; si no, la identidad geografica de la parada."""
    did = row.values.get("delivery_id")
    if not is_blank(did):
        return f"id:{did}"
    lat, lng = row.values.get("lat"), row.values.get("lng")
    if lat is not None and lng is not None:
        return f"geo:{lat:.6f},{lng:.6f}"
    addr = row.values.get("address")
    return f"addr:{str(addr).strip().lower()}" if not is_blank(addr) else f"row:{row.index}"


def assemble(rows: list[NormalizedRow], schema: TargetSchema) -> tuple[list[dict], list[str]]:
    """Devuelve (deliveries anidadas, warnings)."""
    warnings: list[str] = []
    order: list[str] = []
    grouped: dict[str, dict[str, Any]] = {}
    expanded = 0

    delivery_fields = [n for n in schema.column_order
                       if n in schema.fields and schema.fields[n].level == "delivery"]

    for row in rows:
        key = group_key(row, schema)
        if key not in grouped:
            payload = {n: row.values[n] for n in delivery_fields
                       if n in row.values and not is_blank(row.values[n])}
            payload["packages"] = []
            grouped[key] = payload
            order.append(key)
        delivery = grouped[key]

        tw = _time_window(row)
        if not has_package(row, schema):
            if tw:                                   # entrega sin bultos pero con ventana
                delivery.setdefault("time_window", tw)
            continue

        pkg = _package_payload(row, schema)
        if tw:
            pkg["time_window"] = tw

        qty = row.values.get("quantity")
        base = row.values.get("delivery_id") or f"row{row.index}"

        if not is_blank(pkg.get("package_id")):
            delivery["packages"].append(pkg)
            continue

        if not qty or qty <= 1:
            # sin id propio: se sintetiza igual que en la expansion, para que el
            # MISMO pedido escrito de las dos formas de la MISMA estructura
            pkg.setdefault("package_id", f"{base}-{len(delivery['packages']) + 1}")
            delivery["packages"].append(pkg)
            continue

        # forma B: 'bultos: 3' sin package_id -> 3 bultos con id sintetico
        pkg.pop("quantity", None)
        for n in range(1, int(qty) + 1):
            clone = dict(pkg)
            clone["dimensions"] = dict(pkg["dimensions"]) if "dimensions" in pkg else None
            if clone["dimensions"] is None:
                clone.pop("dimensions")
            clone["package_id"] = f"{base}-{n}"
            delivery["packages"].append(clone)
        expanded += 1

    if expanded:
        warnings.append(
            f"{expanded} fila(s) traian cantidad de bultos sin package_id: se expandieron "
            "a un bulto por unidad con id sintetico '<delivery_id>-N'."
        )
    return [grouped[k] for k in order], warnings
