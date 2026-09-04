"""AISchemaMapper: el modelo se usa SOLO para las columnas que las reglas no
resolvieron, y ve unicamente headers + filas de muestra.

Invariantes que no se negocian:
  - el modelo nunca ve el archivo completo (una inferencia por archivo, no por fila)
  - su respuesta se valida contra el schema; si devuelve basura, se ignora
  - si el modelo no carga o tarda de mas, el resultado de reglas sigue siendo valido
"""
from __future__ import annotations

import json
import re

from ..config import Config
from ..readers.base import Table
from ..schemas import TargetSchema
from .base import ColumnMapping, MappingResult
from .mapper import RuleSchemaMapper

AI_CONFIDENCE = 0.85            # por debajo del umbral de auto-aceptacion a proposito
MAX_SAMPLE_CHARS = 4000


class AISchemaMapper:
    """Envuelve al mapper de reglas. Si la IA falla, el resultado de reglas queda."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config.from_env()
        self.rules = RuleSchemaMapper(self.config)

    def detect(self, table: Table, schema: TargetSchema) -> MappingResult:
        result = self.rules.detect(table, schema)

        pending = self._pending(result, table, schema)
        if not pending:
            return result                      # las reglas alcanzaron: no se paga inferencia

        try:
            suggestions = self._ask_model(table, schema, pending, result)
        except Exception as exc:
            result.warnings.append(
                f"el modelo no pudo ejecutarse ({exc}); se sigue con reglas y "
                "las columnas dudosas quedan para revision")
            return result

        self._apply(result, suggestions, schema)
        result.ai_used = bool(suggestions)
        return result

    # ---------- que mandarle ----------

    def _pending(self, result: MappingResult, table: Table,
                 schema: TargetSchema) -> list[str]:
        """Columnas sin mapear o mapeadas por debajo del umbral de auto-aceptacion."""
        ambiguous = {a["column"] for a in result.ambiguous}
        return [c for c in table.columns if c in result.unmapped or c in ambiguous]

    def _prompt(self, table: Table, schema: TargetSchema, pending: list[str],
                result: MappingResult) -> str:
        sample = table.sample(self.config.sample_rows)
        trimmed = [{c: row.get(c) for c in pending} for row in sample]
        payload = json.dumps(trimmed, ensure_ascii=False, default=str)[:MAX_SAMPLE_CHARS]

        already = {col: m.target for col, m in result.mapping.items() if col not in pending}
        fields = {name: f.type for name, f in schema.fields.items()
                  if name not in set(already.values())}

        return (
            "Sos un asistente que mapea columnas de una planilla de entregas a un schema.\n"
            "Devolve UNICAMENTE un objeto JSON {columna: campo}. Si una columna no "
            "corresponde a ningun campo, usa null.\n\n"
            f"Campos disponibles (nombre: tipo):\n{json.dumps(fields, ensure_ascii=False)}\n\n"
            f"Columnas ya resueltas (no las repitas):\n{json.dumps(already, ensure_ascii=False)}\n\n"
            f"Columnas a mapear: {json.dumps(pending, ensure_ascii=False)}\n\n"
            f"Filas de ejemplo:\n{payload}\n\nJSON:"
        )

    def _ask_model(self, table: Table, schema: TargetSchema, pending: list[str],
                   result: MappingResult) -> dict[str, str]:
        from ..models.loader import load

        loaded = load(self.config.model, self.config.device)
        prompt = self._prompt(table, schema, pending, result)

        import torch
        inputs = loaded.tokenizer(prompt, return_tensors="pt", truncation=True, max_length=4096)
        if loaded.device != "cpu":
            inputs = {k: v.to(loaded.device) for k, v in inputs.items()}

        with torch.inference_mode():
            output = loaded.model.generate(**inputs, max_new_tokens=256, do_sample=False)
        text = loaded.tokenizer.decode(output[0][inputs["input_ids"].shape[1]:],
                                       skip_special_tokens=True)
        return self._parse(text)

    # ---------- que hacer con la respuesta ----------

    @staticmethod
    def _parse(text: str) -> dict[str, str]:
        """Extrae el primer objeto JSON. Un modelo chico agrega texto alrededor."""
        try:
            direct = json.loads(text)
            if isinstance(direct, dict):
                return direct
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*?\}", text, re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _apply(self, result: MappingResult, suggestions: dict, schema: TargetSchema) -> None:
        taken = {m.target for m in result.mapping.values()}
        for column, target in suggestions.items():
            if not target or target not in schema.fields:
                continue                       # el modelo invento un campo: se descarta
            if target in taken:
                continue                       # un target, una sola columna
            current = result.mapping.get(column)
            if current and current.confidence >= AI_CONFIDENCE:
                continue                       # las reglas fueron mas seguras
            result.mapping[column] = ColumnMapping(
                column, target, AI_CONFIDENCE, "ai", "sugerido por el modelo de extraccion")
            taken.add(target)
            if column in result.unmapped:
                result.unmapped.remove(column)

        # lo que sugirio la IA sigue yendo a revision: es una sugerencia, no un hecho
        mapped = set(result.mapping)
        result.ambiguous = [a for a in result.ambiguous if a["column"] in mapped]
        for column, m in result.mapping.items():
            if m.method == "ai" and not any(a["column"] == column for a in result.ambiguous):
                result.ambiguous.append({
                    "column": column, "suggested": m.target,
                    "confidence": m.confidence, "method": "ai", "evidence": m.evidence,
                })
