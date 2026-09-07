"""Filas planas -> deliveries con packages.

Dos formas de venir el mismo dato:
  A) una fila POR BULTO, repitiendo los datos de la entrega  -> agrupar
  B) una fila POR ENTREGA con 'bultos: 3'                    -> expandir
Ambas terminan en la misma estructura.
"""
from __future__ import annotations

from typing import Any

from ..normalization.row_normalizer import PASSTHROUGH_PREFIX, NormalizedRow
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


def _geocode_payload(row: NormalizedRow) -> dict[str, Any] | None:
    """`{status, confidence, band, precision, source}` de la fila, si vino geocodificada.

    Viaja en el nested para que el consumidor coloree con la MISMA banda que trae
    el CSV, en vez de recalcularla con sus propios umbrales.
    """
    payload: dict[str, Any] = {}
    for name, value in row.values.items():
        if not name.startswith(PASSTHROUGH_PREFIX) or is_blank(value):
            continue
        key = name[len(PASSTHROUGH_PREFIX):]
        payload[key] = float(value) if key in ("confidence", "raw_score") else value
    return payload or None


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
    """Agrupa bultos en una entrega SOLO por `delivery_id`.

    - Mismo id → un stop con N packages (aunque el address difiera un poco).
    - Distinto id → stops distintos, aunque compartan address o lat/lng
      (un edificio con 20 pedidos = 20 stops, no uno).
    - Sin id → cada fila es su propia entrega (`row:N`). No se fusiona por
      geo/address: eso mezclaba pedidos distintos en el mismo pin.
    """
    did = row.values.get("delivery_id")
    if not is_blank(did):
        return f"id:{str(did).strip()}"
    return f"row:{row.index}"


def assemble(rows: list[NormalizedRow], schema: TargetSchema, *,
             weight_is_total: bool = False) -> tuple[list[dict], list[str]]:
    """Devuelve (deliveries anidadas, warnings).

    `weight_is_total=True` (texto libre): al expandir `quantity` el `weight_kg` se
    interpreta como peso TOTAL de la entrega y se reparte entre los bultos.
    En tabular (default) el peso se clona tal cual — suele ser unitario en Excel.
    """
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
            if geocode := _geocode_payload(row):
                payload["geocode"] = geocode
            payload["packages"] = []
            grouped[key] = payload
            order.append(key)
        else:
            # Completar campos de entrega que la primera fila no trajo (p.ej. address
            # null en un bulto y texto en otro del mismo delivery_id).
            delivery = grouped[key]
            for name in delivery_fields:
                if name in delivery and not is_blank(delivery.get(name)):
                    continue
                value = row.values.get(name)
                if not is_blank(value):
                    delivery[name] = value
            if "geocode" not in delivery:
                if geocode := _geocode_payload(row):
                    delivery["geocode"] = geocode
        delivery = grouped[key]

        # Ventana = atributo del STOP. Tambien se copia al package si hay bulto
        # (compat con consumidores viejos que la leian ahi).
        tw = _time_window(row)
        if tw:
            delivery.setdefault("time_window", tw)

        if not has_package(row, schema):
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
        count = int(qty)
        per_weight = None
        if weight_is_total and not is_blank(pkg.get("weight_kg")) and count > 0:
            per_weight = round(float(pkg["weight_kg"]) / count, 3)
        for n in range(1, count + 1):
            clone = dict(pkg)
            clone["dimensions"] = dict(pkg["dimensions"]) if "dimensions" in pkg else None
            if clone["dimensions"] is None:
                clone.pop("dimensions")
            if per_weight is not None:
                clone["weight_kg"] = per_weight
            clone["package_id"] = f"{base}-{n}"
            delivery["packages"].append(clone)
        expanded += 1

    if expanded:
        warnings.append(
            f"{expanded} fila(s) traian cantidad de bultos sin package_id: se expandieron "
            "a un bulto por unidad con id sintetico '<delivery_id>-N'."
        )
        if weight_is_total:
            warnings.append(
                "peso en texto libre tratado como TOTAL: se repartio entre los bultos "
                "expandidos (en tabular el peso se clona unitario)."
            )
    return [grouped[k] for k in order], warnings
