"""Extraccion progresiva: cada campo resuelto achica la ambiguedad del siguiente.

    CONTACTO: Diego Martinez (1140351035). LUGAR: Darwin 1395. HORARIO: antes de 14hs.
      telefono   -> +541140351035     (y sale del texto)
      etiquetas  -> name, address, delivery_time_text
      lo que queda -> reference

No hay modelo en ningun paso. El orden va de lo mas objetivo (coordenadas, un
numero de telefono valido) a lo mas interpretable (nombre, notas), porque lo
objetivo se puede verificar y lo interpretable conviene decidirlo con el texto ya
despejado.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date

from ..addresses import AddressCandidateScorer, build_address_parser
from .address_extract import extract_address
from .canvas import TextCanvas
from .context import ExtractionContext
from .coordinates import extract_coordinates
from .customer_name import extract_customer_name
from .labels import extract_labeled
from .packages import extract_packages
from .phone_extract import extract_phone
from .reference import extract_email, extract_reference
from .result import INVALID, NEEDS_REVIEW, VALID, ExtractedRecord, FieldValue
from .time_window import extract_time


#: pasos que aportan info pero no se quedan con el texto: la restriccion horaria
#: convive con la nota que la contiene
NON_CONSUMING = frozenset({"time"})


@dataclass
class PipelineStats:
    records: int = 0
    elapsed_s: float = 0.0
    ai_calls: int = 0                      # siempre 0: aca no hay modelo

    def as_dict(self) -> dict:
        return {"records": self.records, "elapsed_s": round(self.elapsed_s, 4),
                "ai_calls": self.ai_calls,
                "records_per_s": int(self.records / self.elapsed_s)
                if self.elapsed_s else 0}


class FieldExtractionPipeline:
    """Corre los extractores en orden sobre un `TextCanvas`."""

    def __init__(self, config=None, context: ExtractionContext | None = None,
                 address_parser=None, scorer: AddressCandidateScorer | None = None):
        self.config = config
        self.context = context or ExtractionContext()
        self.address_parser = address_parser or build_address_parser(
            config, self.context.locales)
        self.scorer = scorer or AddressCandidateScorer(self.context.locales)
        self.stats = PipelineStats()

    # ---------- etapas ----------

    def steps(self) -> tuple[tuple[str, object], ...]:
        """De lo mas objetivo a lo mas interpretable. Visible para tests y debug."""
        return (
            ("coordinates", extract_coordinates),
            ("phone", extract_phone),
            ("email", extract_email),
            ("packages", extract_packages),
            ("labels", extract_labeled),
            ("time", extract_time),
            ("address", self._address),
            ("customer_name", extract_customer_name),
            ("reference", extract_reference),
        )

    def _address(self, canvas, context) -> FieldValue | None:
        return extract_address(canvas, context, scorer=self.scorer,
                               parser=self.address_parser)

    # ---------- corrida ----------

    def run(self, text: str, *, span: tuple[int, int] | None = None,
            service_date: date | None = None,
            timezone: str | None = None) -> ExtractedRecord:
        started = time.perf_counter()
        canvas = TextCanvas(text or "")
        record = ExtractedRecord(source=canvas.original, span=span)

        for name, step in self.steps():
            produced = step(canvas, self.context, service_date, timezone) \
                if name == "time" else step(canvas, self.context)
            for value in _as_list(produced):
                if value is None:
                    continue
                already = value.field in record.fields
                record.set(value)
                if not already and name not in NON_CONSUMING:
                    canvas.consume(value.span)

        self._reconcile(record)
        self._finalize(record)
        self.stats.records += 1
        self.stats.elapsed_s += time.perf_counter() - started
        return record

    # ---------- coherencia ----------

    def _reconcile(self, record: ExtractedRecord) -> None:
        """Arregla solapamientos entre campos sin volver a leer el texto."""
        name = record.get("customer_name")
        address = record.get("address")
        if name and address and address.startswith(name):
            trimmed = address[len(name):].strip(" ,.;:-")
            if trimmed:
                previous = record.fields["address"]
                record.fields["address"] = FieldValue(
                    "address", trimmed, previous.raw, previous.confidence,
                    previous.method, previous.span,
                    (*previous.evidence, "se quito el nombre del cliente del inicio"))

    def _finalize(self, record: ExtractedRecord) -> None:
        """Estado del registro segun la evidencia, no segun si el parser corrio."""
        cfg = self.config
        address_min = getattr(cfg, "address_accept_threshold", 0.50)
        has_coords = record.get("lat") is not None and record.get("lng") is not None
        address_confidence = record.confidence_of("address")

        record.score = max(address_confidence, 0.99 if has_coords else 0.0)
        if has_coords or address_confidence >= address_min:
            record.status = VALID
            return
        if record.get("customer_name") or record.get("phone"):
            record.status = NEEDS_REVIEW
            record.reasons.append(
                "hay cliente o telefono pero la direccion no tiene evidencia suficiente")
            return
        record.status = INVALID
        record.reasons.append("sin destino ni datos de contacto utilizables")


def _as_list(produced) -> list:
    if produced is None:
        return []
    return list(produced) if isinstance(produced, (list, tuple)) else [produced]
