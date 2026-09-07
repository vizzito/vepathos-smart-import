"""Un campo extraido lleva su evidencia, no solo su valor.

Sin `confidence` y `evidence` no se puede decidir si algo va al geocoder, ni
explicarle al operador por que el sistema creyo lo que creyo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: por debajo de esto un campo existe pero pide revision humana
DEFAULT_HIGH_CONFIDENCE = 0.85


@dataclass(frozen=True)
class FieldValue:
    field: str
    value: Any
    raw: str = ""
    confidence: float = 0.0
    method: str = ""
    span: tuple[int, int] | None = None
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "value": self.value, "raw": self.raw,
            "confidence": round(self.confidence, 3), "method": self.method,
            "span": list(self.span) if self.span else None,
            "evidence": list(self.evidence),
        }


VALID = "valid"
NEEDS_REVIEW = "needs_review"
INVALID = "invalid"
IGNORED = "ignored"


@dataclass
class ExtractedRecord:
    """Lo que un segmento de texto libre produjo, con su trazabilidad."""
    source: str = ""
    span: tuple[int, int] | None = None
    status: str = VALID
    score: float = 0.0
    fields: dict[str, FieldValue] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def set(self, value: FieldValue | None) -> None:
        """Gana el primero que llega: los extractores corren de mas objetivo a menos."""
        if value is None or value.value in (None, "", []):
            return
        self.fields.setdefault(value.field, value)

    def get(self, name: str) -> Any:
        found = self.fields.get(name)
        return found.value if found else None

    def confidence_of(self, name: str) -> float:
        found = self.fields.get(name)
        return found.confidence if found else 0.0

    def flat(self) -> dict[str, Any]:
        """Solo los valores: lo que consume el schema Vepathos."""
        return {name: fv.value for name, fv in self.fields.items()}

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "span": list(self.span) if self.span else None,
            "status": self.status,
            "score": round(self.score, 3),
            "fields": {k: v.as_dict() for k, v in self.fields.items()},
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }
