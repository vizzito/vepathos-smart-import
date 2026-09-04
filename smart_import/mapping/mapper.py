"""Orquestador: reglas -> fuzzy -> heuristicas -> (solo si hace falta) IA.

Politica: la IA NO se invoca si las reglas resuelven. Y cuando se invoca, ve
`headers + N filas de muestra`, nunca el archivo entero.
"""
from __future__ import annotations

from ..config import Config
from ..readers.base import Table
from ..schemas import TargetSchema
from . import fuzzy, heuristics, rules
from .base import ColumnMapping, MappingResult

# cuando el nombre y el contenido coinciden en el mismo target, la evidencia es
# mucho mas fuerte que cualquiera de los dos por separado
AGREEMENT_BONUS = 0.08
METHOD_RANK = {"manual": 5, "alias": 4, "normalized": 3, "ai": 2, "fuzzy": 1, "heuristic": 0}


class RuleSchemaMapper:
    """Mapper deterministico. Funciona sin ninguna dependencia de IA."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config.from_env()

    def detect(self, table: Table, schema: TargetSchema) -> MappingResult:
        cfg = self.config
        sample_limit = max(cfg.sample_rows * 10, 200)

        # (columna, target) -> (score, method, evidence)
        scored: dict[tuple[str, str], tuple[float, str, str]] = {}
        name_hits: dict[tuple[str, str], float] = {}
        content_hits: dict[tuple[str, str], float] = {}

        for col in table.columns:
            values = table.column_values(col, limit=sample_limit)
            profile = heuristics.ColumnProfile(values)

            by_name = rules.candidates(col, schema) or []
            if not any(s >= 0.97 for _, s, _, _ in by_name):
                by_name += fuzzy.candidates(col, schema)
            by_content = heuristics.candidates(values, profile)

            for target, score, method, why in by_name:
                name_hits[(col, target)] = max(name_hits.get((col, target), 0.0), score)
                _keep(scored, col, target, score, method, why)
            for target, score, method, why in by_content:
                content_hits[(col, target)] = max(content_hits.get((col, target), 0.0), score)
                _keep(scored, col, target, score, method, why)

        # el nombre dice X y el contenido tambien dice X -> subir la confianza
        for key in set(name_hits) & set(content_hits):
            score, method, why = scored[key]
            scored[key] = (min(0.995, score + AGREEMENT_BONUS), method,
                           f"{why} + confirmado por contenido")

        # el nombre dice X pero el contenido lo contradice fuerte -> bajar
        for (col, target), name_score in name_hits.items():
            if target in ("lat", "lng") and content_hits.get((col, target), 0.0) == 0.0:
                vals = heuristics.ColumnProfile(table.column_values(col, limit=sample_limit))
                if vals.nums and not _in_coord_range(target, vals):
                    score, method, why = scored[(col, target)]
                    scored[(col, target)] = (score * 0.55, method,
                                             f"{why} pero los valores NO estan en rango")

        result = _assign(scored, table, schema, self.config)
        _resolve_coordinate_pair(result, table, scored)
        _flag_composite_columns(result, table, sample_limit)
        _flag_review(result, table, self.config)
        return result


def _in_coord_range(target: str, profile) -> bool:
    """Robusto: se tolera hasta un 10% de valores corruptos antes de desconfiar
    del nombre de la columna."""
    lo, hi = (-90.0, 90.0) if target == "lat" else (-180.0, 180.0)
    return profile.frac_in(lo, hi) >= 0.9


def _keep(scored: dict, col: str, target: str, score: float, method: str, why: str) -> None:
    key = (col, target)
    prev = scored.get(key)
    if prev is None or score > prev[0] or (score == prev[0] and METHOD_RANK[method] > METHOD_RANK[prev[1]]):
        scored[key] = (score, method, why)


def _assign(scored: dict, table: Table, schema: TargetSchema, cfg: Config) -> MappingResult:
    """Asignacion global greedy: una columna -> un target, un target -> una columna."""
    result = MappingResult()
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1][0], METHOD_RANK[kv[1][1]] * -1, kv[0]))

    used_cols: set[str] = set()
    used_targets: set[str] = set()
    for (col, target), (score, method, why) in ranked:
        if col in used_cols or target in used_targets:
            continue
        if score < cfg.review_threshold:
            continue
        result.mapping[col] = ColumnMapping(col, target, score, method, why)
        used_cols.add(col)
        used_targets.add(target)

    result.unmapped = [c for c in table.columns if c not in used_cols]
    return result


def _resolve_coordinate_pair(result: MappingResult, table: Table, scored: dict) -> None:
    """lat y lng son indistinguibles por contenido cuando ambas caen en [-90,90].

    Si quedo una sola asignada y hay otra columna numerica que compite por el par,
    se completa el par por orden de aparicion (lat primero) y se avisa.
    """
    by_target = result.by_target()
    have_lat, have_lng = "lat" in by_target, "lng" in by_target
    if have_lat == have_lng:
        return

    missing = "lng" if have_lat else "lat"
    partner = by_target.get("lat") or by_target.get("lng")
    contenders = [
        (col, s) for (col, tgt), (s, _, _) in scored.items()
        if tgt in ("lat", "lng") and col != partner and col in result.unmapped
    ]
    if not contenders:
        return
    col = max(contenders, key=lambda cs: (cs[1], -table.columns.index(cs[0])))[0]

    # el orden de columnas decide: la primera es lat, la segunda lng
    if table.columns.index(col) < table.columns.index(partner) and missing == "lng":
        result.mapping[partner].target, missing = "lng", "lat"
    result.mapping[col] = ColumnMapping(col, missing, 0.80, "heuristic",
                                        "completado como par de coordenadas por orden de columna")
    result.unmapped.remove(col)
    result.warnings.append(
        f"par lat/lng resuelto por orden de columna: '{partner}' y '{col}'. Verificar si estan invertidas."
    )


def _flag_composite_columns(result: MappingResult, table: Table, limit: int) -> None:
    """Una columna que mezcla varios campos ('Juan Perez - Corrientes 1250 - tel 11...').

    Las reglas la mapean entera a `address`, que es lo mejor que pueden hacer, pero
    el resultado arrastra nombre y telefono adentro de la direccion y eso rompe el
    geocoding. Se baja la confianza para que caiga en revision: es exactamente el
    caso donde el modelo de extraccion aporta.
    """
    for col, m in result.mapping.items():
        if m.target != "address":
            continue
        values = [str(v) for v in table.column_values(col, limit=limit) if v]
        if not values:
            continue
        sample = values[:100]
        with_phone = sum(1 for v in sample if _has_phone_inside(v)) / len(sample)
        long_multi = sum(1 for v in sample if len(v) > 45 and v.count(" ") >= 6) / len(sample)
        if with_phone >= 0.5 or long_multi >= 0.7:
            m.confidence = min(m.confidence, 0.68)
            m.evidence += " | la columna mezcla varios campos (telefono/nombre dentro del texto)"
            result.warnings.append(
                f"'{col}' parece contener varios campos en un solo texto. Se mapeo "
                "entero a 'address'. Para separar nombre/direccion/telefono usa la "
                "accion 'extract' (requiere el modelo)."
            )


def _has_phone_inside(text: str) -> bool:
    import re
    return bool(re.search(r"(?<!\d)(?:\+?\d[\d\s().-]{7,16})(?!\d)", text)) and \
        sum(c.isdigit() for c in text) >= 8


def _flag_review(result: MappingResult, table: Table, cfg: Config) -> None:
    for col, m in result.mapping.items():
        if m.confidence < cfg.auto_accept_threshold:
            result.ambiguous.append({
                "column": col, "suggested": m.target,
                "confidence": round(m.confidence, 3), "method": m.method,
                "evidence": m.evidence,
            })
