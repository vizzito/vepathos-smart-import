"""Extraccion de campos. Determinística: reglas + librerias, sin modelo."""
from .context import ExtractionContext
from .pipeline import FieldExtractionPipeline, PipelineStats
from .result import ExtractedRecord, FieldValue

__all__ = [
    "ExtractionContext", "FieldExtractionPipeline", "PipelineStats",
    "ExtractedRecord", "FieldValue",
]
