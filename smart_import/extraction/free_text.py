"""De un documento de texto libre a filas del schema, sin modelo.

    documento -> segmentar -> clasificar -> extraer campo por campo -> filas

Las dos entradas que necesitan esto son distintas y comparten todo lo demas:

  * un archivo entero que es texto libre (un paste de WhatsApp)
  * UNA columna de un archivo tabular que mezcla varios campos

En los dos casos el trabajo pesado corre solo sobre lo que hace falta: un CSV con
las columnas ya mapeadas no paga nada de esto.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date

from ..addresses import AddressCandidateScorer, build_address_parser
from ..detection.delivery_classifier import DeliveryCandidateClassifier
from ..detection.segmenter import FreeTextSegmenter, Segment
from ..normalization.address import infer_locality_tokens, maximize_address_for_geocode
from .address_parts import fill_address_parts_from_text
from .context import ExtractionContext
from .pipeline import FieldExtractionPipeline
from .result import IGNORED, INVALID, NEEDS_REVIEW, VALID, ExtractedRecord

#: campos que el extractor produce pero que no son columnas del schema Vepathos
NON_SCHEMA_FIELDS = ("delivery_time_text", "email")


@dataclass
class DocumentExtraction:
    """Resultado completo, con lo aceptado Y lo descartado (y por que)."""
    records: list[ExtractedRecord] = field(default_factory=list)
    ignored: list[dict] = field(default_factory=list)
    strategy: str = ""
    segments: int = 0
    elapsed_s: float = 0.0
    ai_calls: int = 0                                   # 0 por diseño
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {VALID: 0, NEEDS_REVIEW: 0, INVALID: 0, IGNORED: len(self.ignored)}
        for record in self.records:
            out[record.status] = out.get(record.status, 0) + 1
        return out

    def field_stats(self, high_confidence: float = 0.85) -> dict[str, dict]:
        stats: dict[str, dict] = {}
        for record in self.records:
            for name, value in record.fields.items():
                entry = stats.setdefault(name, {"detected": 0, "high_confidence": 0})
                entry["detected"] += 1
                if value.confidence >= high_confidence:
                    entry["high_confidence"] += 1
        return stats

    def as_dict(self) -> dict:
        counts = self.counts()
        return {
            "strategy": self.strategy,
            "segments": self.segments,
            "deliveries": len(self.records),
            "valid": counts.get(VALID, 0),
            "needs_review": counts.get(NEEDS_REVIEW, 0),
            "invalid": counts.get(INVALID, 0),
            "ignored": len(self.ignored),
            "ignored_samples": self.ignored[:20],
            "fields": self.field_stats(),
            "processing_time_ms": round(self.elapsed_s * 1000, 1),
            "ai_calls": self.ai_calls,
            "warnings": list(self.warnings),
        }


#: evidencia minima de "hay un lugar real" (no "1 solo sin destinatario")
_STREETISH = re.compile(
    r"\b(av\.?|avenida|calle|callej[oó]n|street|st\.?|road|rd\.?|rua|camino|pasaje|"
    r"blvd|boulevard|lane|alley|strada|v[ií]a|"
    r"manzana|lote|parcela|quadra|plot|sector|flat|apt\.?|unit|house|villa|"
    r"galp[aã]o|cond\.?|condom[ií]nio|bloco|torre)\b",
    re.IGNORECASE)
_DOORISH = re.compile(r"(?<!\d)\d{2,5}(?!\d)")


class FreeTextExtractor:
    """Segmentador + clasificador + pipeline de campos, con una sola config."""

    def __init__(self, config=None, context: ExtractionContext | None = None,
                 service_date: date | None = None,
                 timezone: str | None = None):
        self.config = config
        self.context = context or ExtractionContext.from_config(config)
        self.service_date = service_date
        self.timezone = timezone
        parser = build_address_parser(config, self.context.locales)
        scorer = AddressCandidateScorer(self.context.locales)
        self.segmenter = FreeTextSegmenter()
        self.classifier = DeliveryCandidateClassifier(config, self.context, scorer)
        self.fields = FieldExtractionPipeline(config, self.context, parser, scorer)
        self._doc_locality: list[str] = []

    # ---------- documento completo ----------

    def run_document(self, document: str) -> DocumentExtraction:
        started = time.perf_counter()
        from .text_preprocess import preprocess_free_text_document
        document, prep_notes = preprocess_free_text_document(document or "")
        self._doc_locality = infer_locality_tokens(document)
        segmented = self.segmenter.split(document)
        out = DocumentExtraction(strategy=segmented.strategy)
        out.warnings.extend(prep_notes)
        segments = segmented.all_segments
        out.segments = len(segments)

        # Una sola pasada del clasificador: lo aceptado se extrae, lo descartado
        # se guarda con su motivo. Nada se pierde en silencio.
        for segment in segments:
            verdict = self.classifier.classify(segment.body)
            if not verdict.is_delivery:
                out.ignored.append({"text": segment.text[:200],
                                    "reasons": list(verdict.reasons),
                                    "score": round(verdict.score, 3)})
                continue
            record = self.fields.run(segment.body, span=segment.span,
                                     service_date=self.service_date,
                                     timezone=self.timezone)
            record.reasons.extend(verdict.reasons)
            record.score = max(record.score, verdict.score)
            self._normalize(record)
            if not self._has_actionable_destination(record):
                out.ignored.append({
                    "text": segment.text[:200],
                    "reasons": ["sin telefono ni direccion utilizable "
                                "(bultos solos no alcanzan para una entrega)"],
                    "score": round(record.score, 3),
                })
                continue
            self._assign_id(record, segment, len(out.records) + 1)
            out.records.append(record)

        out.elapsed_s = time.perf_counter() - started
        return out

    # ---------- una columna free-text ----------

    def run_value(self, text: str, position: int = 1) -> ExtractedRecord | None:
        """Una celda que mezcla varios campos. La fila ya existe: no se clasifica."""
        if not (text or "").strip():
            return None
        record = self.fields.run(str(text), service_date=self.service_date,
                                 timezone=self.timezone)
        self._normalize(record)
        return record

    # ---------- post-extraccion ----------

    @staticmethod
    def _assign_id(record: ExtractedRecord, segment: Segment, position: int) -> None:
        """El id lo genera el codigo, no el extractor: nunca se inventa un id del texto."""
        record.fields.pop("delivery_id", None)
        from .result import FieldValue
        value = segment.list_number or f"{position:03d}"
        source = "numero de la lista" if segment.list_number else "posicion en el documento"
        record.fields["delivery_id"] = FieldValue(
            "delivery_id", str(value), "", 1.0, "generated", None, (source,))

    def _has_actionable_destination(self, record: ExtractedRecord) -> bool:
        """Bultos solos no son una entrega: hace falta telefono o direccion usable.

        Tras consumir qty/peso/dims el residual ('1 solo sin destinatario') a veces
        engaña al heuristico; exigimos token de via o altura de 2+ digitos.
        """
        if record.get("phone"):
            return True
        if record.get("lat") is not None and record.get("lng") is not None:
            return True
        address_min = getattr(self.config, "address_accept_threshold", 0.50)
        address = record.get("address")
        if not (address and record.confidence_of("address") >= address_min):
            return False
        return bool(_STREETISH.search(address) or _DOORISH.search(address))

    def _normalize(self, record: ExtractedRecord) -> None:
        """NORMALIZAR != EXTRAER: partes del schema + compose + contexto regional.

        1. Parser → house_number / city / zone / …
        2. Compose address final (misma convencion que tabular)
        3. Completar localidad: depot / pistas del documento / phone_region
           (phone_region NO pisa un pais ya implicado por el texto o el depot)
        """
        fill_address_parts_from_text(
            record, self.fields.address_parser, context=self.context)

        address = record.get("address")
        if not address:
            return
        from .result import FieldValue
        extras = list(getattr(self, "_doc_locality", ()) or ())
        if self.context.city:
            extras.append(self.context.city)
        if self.context.state:
            extras.append(self.context.state)
        if self.context.country:
            extras.append(self.context.country)
        previous = record.fields["address"]
        enriched = maximize_address_for_geocode(
            address,
            phone_region=self.context.phone_region,
            extra_tokens=extras or None,
            country=self.context.country,
        )
        if enriched == address:
            return
        record.fields["address"] = FieldValue(
            "address", enriched, previous.raw, previous.confidence, previous.method,
            previous.span,
            (*previous.evidence, "normalizacion: se completo con contexto geografico"))


# ---------------------------------------------------------------- a Table

def records_to_table(records, schema, meta):
    """Filas del schema a partir de los registros extraidos.

    Las columnas YA son campos del schema, asi que el mapping posterior es la
    identidad: no hay nada que adivinar y la confianza es 1.0.
    """
    from ..mapping.base import ColumnMapping, MappingResult
    from ..readers.base import Table

    present: list[str] = []
    for record in records:
        for name in record.fields:
            if name in schema.fields and name not in present:
                present.append(name)
    columns = [c for c in schema.column_order if c in present]

    rows = []
    for record in records:
        flat = record.flat()
        rows.append(tuple(flat.get(c) for c in columns))

    mapping = MappingResult()
    for column in columns:
        mapping.mapping[column] = ColumnMapping(
            column, column, 1.0, "manual",
            "campo producido por la extraccion determinística de texto libre")
    return Table(meta=meta, columns=columns, rows=rows), mapping


def expand_free_text_column(table, column: str, extractor: "FreeTextExtractor",
                            schema):
    """Aplica el pipeline SOLO a la columna que mezcla varios campos.

    El resto del archivo no se toca: las columnas ya mapeadas siguen el camino
    rapido de siempre (punto 17 del refactor).
    """
    from ..readers.base import Table

    index = table.columns.index(column)
    produced: list[str] = []
    extracted: list[dict] = []
    for row in table.rows:
        record = extractor.run_value(row[index] if index < len(row) else None)
        values = record.flat() if record else {}
        extracted.append(values)
        for name in values:
            if name in schema.fields and name not in produced and name != column:
                produced.append(name)

    columns = [c for c in table.columns if c != column] + [
        c for c in schema.column_order if c in produced]
    keep = [table.columns.index(c) for c in table.columns if c != column]

    rows = []
    for row, values in zip(table.rows, extracted):
        base = tuple(row[i] if i < len(row) else None for i in keep)
        rows.append(base + tuple(values.get(c) for c in columns[len(keep):]))

    table.meta.notes.append(
        f"columna '{column}' detectada como texto libre: se separo en "
        f"{', '.join(columns[len(keep):]) or 'ningun campo'} con reglas (sin modelo)")
    return Table(meta=table.meta, columns=columns, rows=rows)
