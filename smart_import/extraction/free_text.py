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

#: metodos del mapper que se apoyan en el NOMBRE de la columna. Un bloque
#: embebido se acepta como tabla solo si su primera linea NOMBRA campos: la
#: forma sola no distingue un header de la primera entrega de una lista
#: ('Ana Perez | Av. Corrientes 100 | 11 4000-1000' tambien tiene 3 celdas).
HEADER_METHODS = frozenset({"alias", "normalized", "fuzzy"})
#: columnas nombradas minimas, en absoluto y en proporcion
MIN_TABLE_NAMED_COLUMNS = 2
MIN_TABLE_NAMED_RATIO = 0.5
#: un bloque sin ninguna senal de destino no es una tabla de entregas
TABLE_DESTINATION_FIELDS = ("address", "lat", "lng", "customer_name", "phone")


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
    #: bloques tabulares embebidos que se leyeron como tabla, con su header
    tables: list[dict] = field(default_factory=list)

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
            "embedded_tables": list(self.tables),
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
                 timezone: str | None = None, schema=None):
        self.config = config
        self.context = context or ExtractionContext.from_config(config)
        self.service_date = service_date
        self.timezone = timezone
        # Sin schema no hay con que confirmar un header, y un bloque embebido
        # sin confirmar es exactamente el falso positivo que se quiere evitar:
        # la capa se apaga sola y el documento se lee como siempre.
        self.schema = schema
        parser = build_address_parser(config, self.context.locales)
        scorer = AddressCandidateScorer(self.context.locales)
        self.segmenter = FreeTextSegmenter()
        self.classifier = DeliveryCandidateClassifier(config, self.context, scorer)
        self.fields = FieldExtractionPipeline(config, self.context, parser, scorer)
        self._doc_locality: list[str] = []

    # ---------- documento completo ----------

    def run_document(self, document: str) -> DocumentExtraction:
        started = time.perf_counter()
        from ..detection.blocks import blank_spans
        from .text_preprocess import preprocess_free_text_document
        document, prep_notes = preprocess_free_text_document(document or "")
        self._doc_locality = infer_locality_tokens(document)

        # 1. Los bloques que SON una tabla se leen como tabla y salen de la
        #    vista del segmentador (en blanco, sin mover un solo offset).
        tables = self._confirmed_tables(document)
        remaining = blank_spans(document, [b.span for b, _, _ in tables])

        segmented = self.segmenter.split(remaining)
        out = DocumentExtraction(strategy=segmented.strategy)
        out.warnings.extend(prep_notes)
        segments = segmented.all_segments
        out.segments = len(segments)

        # 2. El resto es texto libre, igual que siempre. Una sola pasada del
        #    clasificador: lo aceptado se extrae, lo descartado se guarda con su
        #    motivo. Nada se pierde en silencio.
        produced: list[tuple[int, ExtractedRecord, Segment | None]] = []
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
            produced.append((segment.start, record, segment))

        for block, mapping, columns in tables:
            out.tables.append({**block.as_dict(),
                               "mapped": {c: m.target for c, m in mapping.mapping.items()}})
            out.warnings.append(
                f"bloque tabular embebido: {len(block.rows)} fila(s) con header "
                f"{', '.join(block.header)!r} se leyeron como tabla, no como texto.")
            for span, record in self._table_records(document, block, mapping, columns):
                if not self._has_actionable_destination(record):
                    out.ignored.append({
                        "text": document[span[0]:span[1]][:200],
                        "reasons": ["fila del bloque tabular sin destino utilizable"],
                        "score": round(record.score, 3)})
                    continue
                produced.append((span[0], record, None))
            out.segments += len(block.rows)

        # 3. Un solo orden: el del documento. El id se asigna recien aca, para
        #    que la numeracion siga leyendose de arriba hacia abajo aunque una
        #    parte haya venido de una tabla y otra de una lista escrita a mano.
        for position, (_, record, segment) in enumerate(sorted(
                produced, key=lambda item: item[0]), start=1):
            self._assign_id(record, segment, position)
            out.records.append(record)

        if tables:
            out.strategy = f"{segmented.strategy}+tabla({len(tables)})"
        out.elapsed_s = time.perf_counter() - started
        return out

    # ---------- bloques tabulares embebidos ----------

    def _confirmed_tables(self, document: str):
        """Candidatos estructurales que el SCHEMA confirma como tabla.

        La forma la decide `find_table_blocks`; el significado, el mapper. Un
        bloque pasa solo si su header NOMBRA campos —no alcanza con que el
        contenido se parezca— y si entre esos campos hay alguno de destino.
        """
        if self.schema is None:
            return []
        from ..detection.blocks import find_table_blocks
        from ..mapping import build_mapper
        from ..readers.base import FileMeta, Table, dedupe_columns

        confirmed = []
        for block in find_table_blocks(document, self.context.locales):
            columns = dedupe_columns(list(block.header))
            meta = FileMeta(path="<bloque embebido>", format="txt",
                            delimiter=block.delimiter, text_mode="tabular")
            table = Table(meta=meta, columns=columns,
                          rows=[tuple(r) for r in block.rows])
            mapping = build_mapper(self.config).detect(table, self.schema)
            named = [m for m in mapping.mapping.values() if m.method in HEADER_METHODS]
            if len(named) < MIN_TABLE_NAMED_COLUMNS:
                continue
            if len(named) / max(1, len(columns)) < MIN_TABLE_NAMED_RATIO:
                continue
            if not any(m.target in TABLE_DESTINATION_FIELDS for m in named):
                continue
            mapping.mapping = {c: m for c, m in mapping.mapping.items()
                               if m.method in HEADER_METHODS}
            confirmed.append((block, mapping, columns))
        return confirmed

    def _table_records(self, document: str, block, mapping, columns):
        """Una fila del bloque -> un registro, con los campos que el header dijo."""
        from .result import FieldValue
        index = {name: i for i, name in enumerate(columns)}

        for cells, span in zip(block.rows, block.row_spans):
            record = ExtractedRecord(source=document[span[0]:span[1]], span=span)
            for column, column_mapping in mapping.mapping.items():
                position = index.get(column)
                value = cells[position] if position is not None and position < len(cells) else None
                if value is None or not str(value).strip():
                    continue
                clean = self._table_value(column_mapping.target, str(value).strip())
                if not clean:
                    continue
                record.set(FieldValue(
                    column_mapping.target, clean, str(value),
                    column_mapping.confidence, "embedded_table", span,
                    (f"columna '{column}' del bloque tabular embebido",)))
            self.fields._finalize(record)
            self._normalize(record)
            yield span, record

    def _table_value(self, target: str, value: str) -> str:
        """El mismo tratamiento que le daria el texto libre a ese campo.

        Un telefono que sale de una celda tiene que quedar igual que uno que
        sale de una frase: si no, el mismo documento emite dos formatos segun
        de que mitad vino la fila.
        """
        if target != "phone":
            return value
        from ..normalization.phone import normalize
        normalized, _ = normalize(value, self.context.phone_region)
        return normalized or value


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
    def _assign_id(record: ExtractedRecord, segment: Segment | None,
                   position: int) -> None:
        """El id lo genera el codigo, no el extractor: nunca se inventa un id del texto.

        La excepcion es una COLUMNA de id en un bloque tabular embebido: ahi el
        header dijo que eso es el id, y pisarlo seria tirar el dato del cliente.
        """
        from .result import FieldValue
        existing = record.fields.get("delivery_id")
        if existing is not None and existing.method == "embedded_table":
            return
        record.fields.pop("delivery_id", None)
        list_number = segment.list_number if segment else None
        value = list_number or f"{position:03d}"
        source = "numero de la lista" if list_number else "posicion en el documento"
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

    Esta funcion solo corre cuando el mapper mapeo esa columna a ``address``.
    Al reemplazarla hay que dejar un ``address`` en la tabla: el extraido o,
    si el extractor no lo produjo, el texto original de la celda. Si no, el
    geocoder nunca se ofrece (needs_geocode=0).
    """
    from ..readers.base import Table

    index = table.columns.index(column)
    produced: list[str] = []
    extracted: list[dict] = []
    for row in table.rows:
        raw = row[index] if index < len(row) else None
        record = extractor.run_value(raw)
        values = record.flat() if record else {}
        if _blank_cell(values.get("address")) and not _blank_cell(raw):
            values["address"] = raw
        extracted.append(values)
        for name in values:
            if name in schema.fields and name not in produced:
                produced.append(name)
    if "address" not in produced:
        produced.append("address")

    remaining = [c for c in table.columns if c != column]
    extra = [c for c in schema.column_order if c in produced and c not in remaining]
    columns = remaining + extra
    keep = [table.columns.index(c) for c in remaining]

    rows = []
    for row, values in zip(table.rows, extracted):
        base = tuple(row[i] if i < len(row) else None for i in keep)
        extras = tuple(
            (row[index] if index < len(row) else None)
            if name == "address" and _blank_cell(values.get(name))
            else values.get(name)
            for name in extra
        )
        rows.append(base + extras)

    table.meta.notes.append(
        f"columna '{column}' detectada como texto libre: se separo en "
        f"{', '.join(extra) or 'ningun campo'} con reglas (sin modelo)")
    return Table(meta=table.meta, columns=columns, rows=rows)


def _blank_cell(value) -> bool:
    return value is None or (isinstance(value, str) and not str(value).strip())
