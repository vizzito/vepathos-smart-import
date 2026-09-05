"""Aplica el mapping a TODAS las filas y valida. Aca no hay IA ni geocoding."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..mapping.base import MappingResult
from ..readers.base import Table
from ..schemas import TargetSchema
from . import phone as phone_mod
from .values import coerce, is_blank

STATUS_OK = "ok"
STATUS_NEEDS_GEOCODE = "needs_geocode"
STATUS_INVALID = "invalid"


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
        out = {STATUS_OK: 0, STATUS_NEEDS_GEOCODE: 0, STATUS_INVALID: 0}
        for r in self.rows:
            out[r.status] = out.get(r.status, 0) + 1
        return out


class RowNormalizer:
    def __init__(self, schema: TargetSchema, phone_region: str | None = None,
                 derive_volume: bool = False):
        self.schema = schema
        self.phone_region = phone_region
        # Derivar volumen AGREGA una columna que el cliente no mando. Por defecto
        # no se hace: el output tiene que reflejar lo que vino en el archivo.
        self.derive_volume = derive_volume

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

        for i, raw_row in enumerate(table.rows, start=1):
            values: dict[str, Any] = {}
            for target in targets:
                idx = col_index.get(by_target[target])
                raw = raw_row[idx] if idx is not None and idx < len(raw_row) else None
                values[target] = coerce(raw, self.schema.fields[target].type)

            if all(is_blank(v) for v in values.values()):
                out.skipped_empty += 1
                continue

            row = NormalizedRow(index=i, values=values)
            self._validate_coordinates(row, warn)
            self._validate_time_window(row, warn)
            self._normalize_phone(row)
            self._derive_volume(row, derivable, warn)
            self._classify(row)
            out.rows.append(row)

        for name in derivable:
            if any(name in r.values for r in out.rows):
                out.targets_present.append(name)
        out.targets_present = [t for t in self.schema.column_order if t in set(out.targets_present)]
        return out

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
            warn("Hay filas con ventana horaria invertida; se descartaron esas ventanas.")

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
        elif has_address:
            row.status = STATUS_NEEDS_GEOCODE
        else:
            row.status = STATUS_INVALID
            row.flag("address", "fila no localizable: sin coordenadas validas "
                                "y sin direccion")
