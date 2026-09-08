"""Aplica el mapping a TODAS las filas y valida. Aca no hay IA ni geocoding."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..mapping.base import MappingResult
from ..readers.base import Table
from ..schemas import TargetSchema
from . import phone as phone_mod
from .address import apply_composed_address
from .units import column_declares_pounds, pounds_to_kg
from .values import coerce, is_blank

STATUS_OK = "ok"
STATUS_NEEDS_GEOCODE = "needs_geocode"
STATUS_INVALID = "invalid"
#: La fila no parece una entrega: filas de totales, notas al pie, fragmentos.
#: Se separa de `invalid` a proposito. Contar el pie de pagina del cliente como
#: "entrega fallida" hace parecer roto un archivo que esta bien.
STATUS_IGNORED = "ignored"

#: Campos que identifican un DESTINO. Sin ninguno de estos, la fila no es un
#: intento de entrega: un delivery_id suelto puede ser "Totales:" y una cantidad
#: suelta puede ser la suma del pie.
IDENTITY_FIELDS = ("address", "lat", "lng", "customer_name", "phone")

#: Columnas que NO son del schema pero tienen que sobrevivir el round-trip.
#: Sin esto, regenerar el nested despues de geocodificar tira `geocode_band` y
#: la UI se queda sin con que colorear.
PASSTHROUGH_PREFIX = "geocode_"

#: Campos de bulto que una celda escrita a mano puede traer mezclados.
#: `coerce` sobre "3 cajas de 10 lb c/u" devuelve 310 —concatena los digitos—,
#: asi que cuando la celda tiene palabras manda el parser de paqueteria.
PACKAGE_TEXT_FIELDS = ("quantity", "weight_kg", "packaging",
                       "length_cm", "width_cm", "height_cm", "volume_cm3")
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


@dataclass
class RowIssue:
    """Un problema concreto de una fila, atado al campo que lo causo.

    Guardar el campo permite que la UI marque la columna exacta en vez de
    mostrar un texto suelto que el operador tiene que interpretar.
    """
    field: str | None
    message: str
    severity: str = "error"          # error | warning

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "message": self.message, "severity": self.severity}

    def __contains__(self, needle: str) -> bool:
        return needle in self.message

    def __str__(self) -> str:
        return f"{self.field}: {self.message}" if self.field else self.message


@dataclass
class NormalizedRow:
    index: int                                   # fila original (1-based, sin contar header)
    values: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_OK
    issues: list[RowIssue] = field(default_factory=list)

    def get(self, name: str) -> Any:
        return self.values.get(name)

    def flag(self, field_name: str | None, message: str, severity: str = "error") -> None:
        self.issues.append(RowIssue(field=field_name, message=message, severity=severity))

    @property
    def messages(self) -> list[str]:
        return [i.message for i in self.issues]

    @property
    def problem_fields(self) -> list[str]:
        """Columnas del schema involucradas, sin repetir y en orden."""
        out: list[str] = []
        for issue in self.issues:
            if issue.field and issue.field not in out:
                out.append(issue.field)
        return out


@dataclass
class NormalizeOutcome:
    rows: list[NormalizedRow] = field(default_factory=list)
    skipped_empty: int = 0
    warnings: list[str] = field(default_factory=list)
    targets_present: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {STATUS_OK: 0, STATUS_NEEDS_GEOCODE: 0, STATUS_INVALID: 0, STATUS_IGNORED: 0}
        for r in self.rows:
            out[r.status] = out.get(r.status, 0) + 1
        return out


class RowNormalizer:
    def __init__(self, schema: TargetSchema, phone_region: str | None = None,
                 derive_volume: bool = False, timezone: str | None = None):
        self.schema = schema
        self.phone_region = phone_region
        # Derivar volumen AGREGA una columna que el cliente no mando. Por defecto
        # no se hace: el output tiene que reflejar lo que vino en el archivo.
        self.derive_volume = derive_volume
        self.timezone = timezone

    def run(self, table: Table, mapping: MappingResult) -> NormalizeOutcome:
        by_target = mapping.by_target()                  # target -> columna origen
        targets = [t for t in self.schema.column_order if t in by_target]
        derivable = ([f.name for f in self.schema.fields.values()
                      if f.derivable and f.name not in targets]
                     if self.derive_volume else [])

        out = NormalizeOutcome(targets_present=list(targets))
        col_index = {c: i for i, c in enumerate(table.columns)}
        warn_seen: set[str] = set()

        def warn(msg: str) -> None:
            if msg not in warn_seen:
                warn_seen.add(msg)
                out.warnings.append(msg)

        passthrough = [c for c in table.columns if c.startswith(PASSTHROUGH_PREFIX)]

        weight_src = by_target.get("weight_kg")
        convert_lb = bool(weight_src and column_declares_pounds(weight_src))
        converted_lb_rows = 0
        package_sources = [t for t in PACKAGE_TEXT_FIELDS if t in by_target]

        for i, raw_row in enumerate(table.rows, start=1):
            values: dict[str, Any] = {}
            for target in targets:
                idx = col_index.get(by_target[target])
                raw = raw_row[idx] if idx is not None and idx < len(raw_row) else None
                values[target] = coerce(raw, self.schema.fields[target].type)

            # La decision de "fila vacia" mira SOLO los campos del schema: un
            # `geocode_status` suelto no convierte una fila vacia en una entrega.
            if all(is_blank(v) for v in values.values()):
                out.skipped_empty += 1
                continue

            if convert_lb and values.get("weight_kg") is not None:
                values["weight_kg"] = pounds_to_kg(values["weight_kg"])
                converted_lb_rows += 1

            row = NormalizedRow(index=i, values=values)
            self._read_package_text(row, raw_row, col_index, by_target,
                                    package_sources, warn)

            # Partes mapeadas (house_number/city/…) → address unica para UI + geocode.
            apply_composed_address(values)

            for column in passthrough:
                idx = col_index.get(column)
                raw = raw_row[idx] if idx is not None and idx < len(raw_row) else None
                if not is_blank(raw):
                    values[column] = str(raw).strip()

            self._validate_coordinates(row, warn)
            self._validate_time_window(row, warn)
            self._apply_timezone(row)
            self._normalize_phone(row)
            self._derive_volume(row, derivable, warn)
            self._classify(row)
            out.rows.append(row)

        if convert_lb and converted_lb_rows:
            warn(
                f"Columna '{weight_src}' interpretada como libras → convertida a "
                f"weight_kg (×{0.45359237:g}) en {converted_lb_rows} fila(s)."
            )

        for name in derivable:
            if any(name in r.values for r in out.rows):
                out.targets_present.append(name)
        out.targets_present = [t for t in self.schema.column_order if t in set(out.targets_present)]
        return out

    # ---------- celdas de bulto escritas a mano ----------

    def _read_package_text(self, row: NormalizedRow, raw_row, col_index,
                           by_target, package_sources, warn) -> None:
        """Una celda de bulto con palabras la lee el parser de paqueteria.

        Solo entra si la celda TIENE letras: una columna 'Bultos' con 2 sigue el
        camino de siempre y ni toca esta capa. Cuando si entra, el parser manda
        sobre `coerce`, porque "3 cajas de 10 lb c/u" coercionado da 310.

        En tabular el peso de la columna es UNITARIO (`assemble` lo clona), asi
        que de la frase se toma el peso por bulto, no el total. Es la misma
        lectura que en texto libre, con la convencion del otro lado.
        """
        if not package_sources:
            return
        from ..packages import parse_packages

        for target in package_sources:
            index = col_index.get(by_target[target])
            raw = raw_row[index] if index is not None and index < len(raw_row) else None
            if not isinstance(raw, str) or not _HAS_LETTER.search(raw):
                continue
            parse = parse_packages(raw)
            if parse.is_empty:
                continue
            self._apply_package_parse(row, parse, raw, by_target, warn)

    def _apply_package_parse(self, row: NormalizedRow, parse, raw: str,
                             by_target, warn) -> None:
        """Pisa lo que la frase dice; completa lo que la fila no traia."""
        wanted = {
            "quantity": parse.quantity,
            "weight_kg": parse.weight_per_unit_kg,
            "packaging": parse.packaging,
            "length_cm": parse.length_cm,
            "width_cm": parse.width_cm,
            "height_cm": parse.height_cm,
            "volume_cm3": parse.volume_cm3,
        }
        for target, value in wanted.items():
            # No se inventan columnas: si el archivo no mapeo `packaging`, el
            # tipo de bulto no aparece de la nada en la salida.
            if value is None or target not in by_target:
                continue
            current = row.values.get(target)
            if current == value:
                continue
            if not is_blank(current):
                row.flag(target,
                         f"la celda decia {raw.strip()!r}: se leyo {value!r} en vez "
                         f"de {current!r}", severity="warning")
                warn("Hay celdas de bulto escritas en texto ('3 cajas de 10 lb c/u'). "
                     "Se leyeron con el parser de paqueteria; revisar las filas marcadas.")
            row.values[target] = value

    # ---------- validaciones ----------

    def _validate_coordinates(self, row: NormalizedRow, warn) -> None:
        lat, lng = row.values.get("lat"), row.values.get("lng")
        if lat is None and lng is None:
            return

        lat_ok = lat is not None and -90 <= lat <= 90
        lng_ok = lng is not None and -180 <= lng <= 180

        # invertidas: la latitud excede 90 pero cabe como longitud, y viceversa.
        # Se AVISA, no se corrige solo: invertir en silencio manda entregas a otro pais.
        if (lat is not None and lng is not None and not lat_ok
                and -180 <= lat <= 180 and -90 <= lng <= 90):
            row.flag("lat", f"lat={lat} fuera de [-90,90] pero lng={lng} si cabe como "
                             "latitud: posible inversion lat/lng (no se corrigio sola)")
            warn("Se detectaron filas con lat/lng posiblemente invertidas. "
                 "Revisar antes de importar; no se corrigieron automaticamente.")

        if lat is not None and not lat_ok:
            row.flag("lat", f"latitud fuera de rango [-90,90]: {lat}")
            row.values["lat"] = None
        if lng is not None and not lng_ok:
            row.flag("lng", f"longitud fuera de rango [-180,180]: {lng}")
            row.values["lng"] = None

        if (row.values.get("lat") is None) != (row.values.get("lng") is None):
            row.flag("lat", "coordenada incompleta: hace falta lat Y lng")
            row.values["lat"] = row.values["lng"] = None

    def _validate_time_window(self, row: NormalizedRow, warn) -> None:
        start, end = row.values.get("tw_start"), row.values.get("tw_end")
        if start is None and end is None:
            return
        if start is None or end is None:
            row.flag("tw_start", "ventana horaria incompleta (falta start o end): "
                                 "se descarto la ventana", severity="warning")
            row.values["tw_start"] = row.values["tw_end"] = None
            row.values.pop("tw_timezone", None)
            warn("Hay filas con ventana horaria incompleta; se descartaron esas ventanas.")
            return
        if start > end:
            row.flag("tw_start", f"ventana horaria invertida ({start} > {end}): "
                                 "se descarto la ventana", severity="warning")
            row.values["tw_start"] = row.values["tw_end"] = None
            row.values.pop("tw_timezone", None)
            warn("Hay filas con ventana horaria invertida; se descartaron esas ventanas.")

    def _apply_timezone(self, row: NormalizedRow) -> None:
        """Si hay TZ de settings/depot, las horas naive se emiten en UTC.

        No inventa timezone si la ventana no cerro (start+end).
        """
        if not self.timezone:
            return
        start, end = row.values.get("tw_start"), row.values.get("tw_end")
        if not start or not end:
            return
        from ..extraction.tz import UTC_NAME, convert_naive_stamp
        converted_start = convert_naive_stamp(str(start), self.timezone)
        converted_end = convert_naive_stamp(str(end), self.timezone)
        if converted_start is None or converted_end is None:
            return
        row.values["tw_start"] = converted_start
        row.values["tw_end"] = converted_end
        row.values["tw_timezone"] = UTC_NAME

    def _normalize_phone(self, row: NormalizedRow) -> None:
        if "phone" not in row.values:
            return
        value, issue = phone_mod.normalize(row.values.get("phone"), self.phone_region)
        row.values["phone"] = value
        if issue:
            row.flag("phone", issue, severity="warning")

    def _derive_volume(self, row: NormalizedRow, derivable: list[str], warn) -> None:
        if not self.derive_volume:
            return
        if "volume_cm3" not in derivable and row.values.get("volume_cm3") is not None:
            return
        dims = [row.values.get(k) for k in ("length_cm", "width_cm", "height_cm")]
        if all(d is not None for d in dims):
            row.values["volume_cm3"] = dims[0] * dims[1] * dims[2]
            warn("volume_cm3 no venia en el archivo: se calculo como largo x ancho x alto.")

    def _classify(self, row: NormalizedRow) -> None:
        has_coords = row.values.get("lat") is not None and row.values.get("lng") is not None
        has_address = not is_blank(row.values.get("address"))
        if has_coords:
            row.status = STATUS_OK
            return
        if has_address:
            row.status = STATUS_NEEDS_GEOCODE
            return

        # Sin destino Y sin ninguna sena de identidad: no es una entrega fallida,
        # es una fila que no era una entrega (totales, nota al pie, fragmento).
        if not any(not is_blank(row.values.get(f)) for f in IDENTITY_FIELDS):
            row.status = STATUS_IGNORED
            row.flag(None, "la fila no parece una entrega (sin direccion, coordenadas, "
                           "cliente ni telefono): se ignora", severity="warning")
            return

        row.status = STATUS_INVALID
        row.flag("address", "entrega sin destino: falta la direccion y no hay "
                            "coordenadas validas")
