"""Separa una columna que mezcla varios campos, usando NuExtract.

Este es el UNICO lugar donde entra el modelo, y es el trabajo para el que
NuExtract fue entrenado: dado un template JSON y un texto, completar el template.

    "Ana Rodriguez - Av. Corrientes 944, Buenos Aires - tel 1136251563"
        -> {"customer_name": "Ana Rodriguez",
            "address": "Av. Corrientes 944, Buenos Aires",
            "phone": "1136251563"}

Por que NO se usa el modelo para mapear columnas: mapear headers a campos es una
tarea de instruccion, y un modelo de 0.5B entrenado para extraccion devuelve el
schema de vuelta en lugar de razonar sobre el. Las reglas + heuristicas resuelven
ese trabajo mejor, mas rapido y sin dependencias.

Costo: ~1 s por fila en CPU. Es una operacion OPT-IN, con tope de filas y
pensada para correr en segundo plano, igual que el geocoding.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field

from ..config import Config
from ..logging_setup import detail, get_logger, stage

logger = get_logger("extract")

# campos que tiene sentido extraer de un texto libre de entregas
DEFAULT_TEMPLATE_FIELDS = ("customer_name", "address", "phone")
MAX_NEW_TOKENS = 160
_JSON_RE = re.compile(r"\{.*?\}", re.S)


@dataclass
class ExtractionResult:
    rows: int = 0
    extracted: int = 0
    failed: int = 0
    elapsed_s: float = 0.0
    fields: tuple[str, ...] = ()
    values: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rows": self.rows, "extracted": self.extracted, "failed": self.failed,
            "fields": list(self.fields),
            "elapsed_s": round(self.elapsed_s, 2),
            "seconds_per_row": round(self.elapsed_s / self.rows, 2) if self.rows else 0.0,
            "warnings": self.warnings,
        }


class CompositeExtractor:
    """Carga el modelo UNA vez y extrae fila por fila."""

    def __init__(self, config: Config | None = None,
                 fields: tuple[str, ...] = DEFAULT_TEMPLATE_FIELDS):
        self.config = config or Config.from_env()
        self.fields = tuple(fields)
        self._loaded = None

    @property
    def template(self) -> dict[str, str]:
        return {name: "" for name in self.fields}

    def _ensure_model(self):
        if self._loaded is None:
            from ..models.loader import load
            self._loaded = load(self.config.model, self.config.device)
        return self._loaded

    def _prompt(self, text: str) -> str:
        """Formato NATIVO de NuExtract: template + texto. No es un chat."""
        return (f"<|input|>\n### Template:\n{json.dumps(self.template, indent=4)}\n"
                f"### Text:\n{text}\n\n<|output|>")

    def extract_one(self, text: str) -> dict[str, str]:
        return self._verify(self._generate(text), text)

    def _generate(self, text: str) -> dict[str, str]:
        import torch

        loaded = self._ensure_model()
        inputs = loaded.tokenizer(self._prompt(text), return_tensors="pt",
                                  truncation=True, max_length=2048)
        if loaded.device != "cpu":
            inputs = {k: v.to(loaded.device) for k, v in inputs.items()}
        with torch.inference_mode():
            output = loaded.model.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS,
                                           do_sample=False)
        raw = loaded.tokenizer.decode(output[0][inputs["input_ids"].shape[1]:],
                                      skip_special_tokens=True)
        return self._parse(raw)

    @staticmethod
    def _fold(text: str) -> str:
        """Sin acentos y en minusculas, para comparar sin depender de la tilde."""
        decomposed = unicodedata.normalize("NFKD", text.lower())
        return "".join(c for c in decomposed if not unicodedata.combining(c))

    def _verify(self, values: dict[str, str], source: str) -> dict[str, str]:
        """Todo valor extraido TIENE que estar en el texto original.

        Un modelo de 0.5B corrompe acentos al regenerar el texto ('Rodriguez' ->
        'Rodrnez'). Como esto es extraccion y no generacion, el valor correcto
        esta en la fuente: se busca ahi y se devuelve el span ORIGINAL, con sus
        tildes intactas. Lo que no aparece en la fuente se descarta: es
        alucinacion, no extraccion.
        """
        folded_source = self._fold(source)
        verified: dict[str, str] = {}

        for key, value in values.items():
            if not value:
                continue
            folded = self._fold(value)
            position = folded_source.find(folded)
            if position >= 0:
                # se devuelve el texto TAL CUAL vino en la fuente
                verified[key] = source[position:position + len(folded)].strip(" -,;|")
                continue

            recovered = self._recover(value, source)
            if recovered:
                verified[key] = recovered

        return verified

    def _recover(self, value: str, source: str) -> str | None:
        """El modelo devolvio algo que no esta literal en la fuente.

        Se intenta reconstruir palabra por palabra: sirve para el caso tipico en
        que solo se corrompio un caracter acentuado. Si ni asi aparece, se
        descarta el valor.
        """
        words = [w for w in re.split(r"\s+", value) if w]
        if not words:
            return None
        folded_source = self._fold(source)
        spans: list[str] = []
        for word in words:
            folded = self._fold(word)
            position = folded_source.find(folded)
            if position >= 0:
                spans.append(source[position:position + len(folded)])
                continue
            # La corrupcion suele estar en el MEDIO de la palabra ('Rodriguez'
            # -> 'Rodrnez'), asi que buscar por prefijo no alcanza. Se busca la
            # palabra mas parecida entre las de la fuente.
            best = self._closest_word(word, source)
            if best:
                spans.append(best)
                continue
            return None
        recovered = " ".join(spans).strip(" -,;|")
        return recovered or None

    #: 'rodrnez' vs 'rodriguez' puntua 0.75: una tilde corrompida cuesta mas
    #: similitud de la que parece. El prefijo comun es lo que evita que este
    #: umbral bajo acepte una palabra que en realidad es otra.
    MIN_WORD_SCORE = 0.70
    MIN_COMMON_PREFIX = 3

    @staticmethod
    def _closest_word(word: str, source: str) -> str | None:
        """Palabra de la fuente mas parecida a la que devolvio el modelo."""
        from rapidfuzz import fuzz

        candidates = [w for w in re.split(r"[\s,;|()]+", source) if len(w) >= 3]
        if not candidates:
            return None
        folded = CompositeExtractor._fold(word)
        best, best_score = None, 0.0
        for candidate in candidates:
            other = CompositeExtractor._fold(candidate)
            prefix = len(os.path.commonprefix([folded, other]))
            if prefix < min(CompositeExtractor.MIN_COMMON_PREFIX, len(folded)):
                continue
            score = fuzz.ratio(folded, other) / 100.0
            if score > best_score:
                best, best_score = candidate, score
        return best if best_score >= CompositeExtractor.MIN_WORD_SCORE else None

    def _parse(self, raw: str) -> dict[str, str]:
        """Solo se aceptan las claves del template; lo demas se descarta."""
        payload = None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            if match := _JSON_RE.search(raw):
                try:
                    payload = json.loads(match.group(0))
                except json.JSONDecodeError:
                    payload = None
        if not isinstance(payload, dict):
            return {}
        return {k: str(v).strip() for k, v in payload.items()
                if k in self.fields and v not in (None, "", [])}

    def run(self, texts: list[str], max_rows: int | None = None,
            progress=None) -> ExtractionResult:
        result = ExtractionResult(fields=self.fields)
        subset = texts[:max_rows] if max_rows else texts
        result.rows = len(subset)
        started = time.perf_counter()
        if max_rows and len(texts) > max_rows:
            result.warnings.append(
                f"se procesaron {max_rows} de {len(texts)} filas "
                "(tope SMART_IMPORT_EXTRACT_MAX_ROWS)")

        stage(logger, "EXTRACT", "cargando modelo (una vez por proceso)",
              modelo=self.config.model, device=self.config.device,
              campos=",".join(self.fields), filas=result.rows)
        try:
            loaded = self._ensure_model()
            seconds = getattr(loaded, "load_seconds", None)
            stage(logger, "EXTRACT", "modelo listo",
                  carga=f"{seconds:.1f}s" if seconds is not None else None)
        except Exception as exc:
            # sin modelo NO se rompe nada: la columna queda como estaba
            result.warnings.append(f"el modelo no esta disponible ({exc}); "
                                   "la columna compuesta queda sin separar")
            result.failed = result.rows
            result.values = [{} for _ in subset]
            return result

        for i, text in enumerate(subset, start=1):
            if not text or not str(text).strip():
                result.values.append({})
                continue
            try:
                values = self.extract_one(str(text))
            except Exception as exc:
                result.values.append({})
                result.failed += 1
                if len(result.warnings) < 3:
                    result.warnings.append(f"fila {i}: {exc}")
                continue
            result.values.append(values)
            if values:
                result.extracted += 1
            if i <= 8:
                detail(logger, f"{str(text)[:44]:<46} -> " +
                       " | ".join(f"{k}={v}" for k, v in values.items()) or "(nada)")
            if progress and i % 10 == 0:
                progress(i, result)

        result.elapsed_s = time.perf_counter() - started
        stage(logger, "DONE", "extract terminado", extraidas=result.extracted,
              fallidas=result.failed, t=f"{result.elapsed_s:.1f}s",
              s_por_fila=f"{result.elapsed_s / result.rows:.2f}" if result.rows else None)
        return result


def find_composite_column(report: dict) -> str | None:
    """La columna que el mapper marco como 'mezcla varios campos', si la hay."""
    for column, info in (report.get("mapping") or {}).items():
        if "varios campos" in (info.get("evidence") or ""):
            return column
    return None
