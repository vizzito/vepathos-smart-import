"""Contrato del mapper. El resto del sistema no sabe que implementacion corre."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..schemas import TargetSchema


@dataclass
class ColumnMapping:
    column: str
    target: str
    confidence: float
    method: str                      # alias | normalized | fuzzy | heuristic | ai | manual
    evidence: str = ""
    unit: str | None = None          # unidad declarada por el usuario (canonica: lb, in, l, ...)
    format: str | None = None        # formato declarado: strptime o decimal_comma / decimal_point

    def as_dict(self) -> dict[str, Any]:
        d = {"target": self.target, "confidence": round(self.confidence, 3), "method": self.method}
        if self.evidence:
            d["evidence"] = self.evidence
        if self.unit:
            d["unit"] = self.unit
        if self.format:
            d["format"] = self.format
        return d


@dataclass
class MappingResult:
    mapping: dict[str, ColumnMapping] = field(default_factory=dict)   # columna -> mapping
    unmapped: list[str] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ai_used: bool = False

    @property
    def needs_review(self) -> bool:
        return bool(self.ambiguous)

    def target_for(self, column: str) -> str | None:
        m = self.mapping.get(column)
        return m.target if m else None

    def by_target(self) -> dict[str, str]:
        return {m.target: col for col, m in self.mapping.items()}

    def as_dict(self) -> dict[str, Any]:
        return {
            "mapping": {c: m.as_dict() for c, m in self.mapping.items()},
            "unmapped": self.unmapped,
            "ambiguous": self.ambiguous,
            "needs_review": self.needs_review,
            "ai_used": self.ai_used,
            "warnings": self.warnings,
        }


class SchemaMapper(Protocol):
    def detect(self, table: Any, schema: TargetSchema) -> MappingResult: ...
