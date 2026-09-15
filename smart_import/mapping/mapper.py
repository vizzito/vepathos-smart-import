"""Orquestador: reglas -> fuzzy -> heuristicas -> (solo si hace falta) IA.

Politica: la IA NO se invoca si las reglas resuelven. Y cuando se invoca, ve
`headers + N filas de muestra`, nunca el archivo entero.
"""
from __future__ import annotations

from ..config import Config
from ..readers.base import Table
from ..resources import ambiguous_aliases
from ..schemas import TargetSchema, normalize_key
from . import fuzzy, heuristics, rules
from .base import ColumnMapping, MappingResult

# cuando el nombre y el contenido coinciden en el mismo target, la evidencia es
# mucho mas fuerte que cualquiera de los dos por separado
AGREEMENT_BONUS = 0.08
#: Cuanto se castiga un match por PARECIDO cuando el contenido de la columna
#: apunta a otro campo con evidencia propia. No se descarta: se manda abajo del
#: piso de asignacion, y sigue visible en el reporte con su motivo.
FUZZY_CONTRADICTED = 0.6
METHOD_RANK = {"manual": 5, "alias": 4, "normalized": 3, "ai": 2, "fuzzy": 1, "heuristic": 0}


class RuleSchemaMapper:
    """Mapper deterministico. Funciona sin ninguna dependencia de IA."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config.from_env()

    def detect(self, table: Table, schema: TargetSchema) -> MappingResult:
        # import tardio: row_normalizer importa mapping.base (ciclo)
        from ..normalization.row_normalizer import PASSTHROUGH_PREFIX

        cfg = self.config
        sample_limit = max(cfg.sample_rows * 10, 200)

        # (columna, target) -> (score, method, evidence)
        scored: dict[tuple[str, str], tuple[float, str, str]] = {}
        name_hits: dict[tuple[str, str], float] = {}
        content_hits: dict[tuple[str, str], float] = {}

        for col in table.columns:
            # `geocode_*` son diagnostico del geocoder que viaja en el round-trip
            # (ver PASSTHROUGH_PREFIX). No son datos del cliente: sin esto, al
            # regenerar el nested, `geocode_confidence` (0.8..1.0) se mapeaba a
            # `weight_kg` y cada entrega geocodificada ganaba un bulto de ~1 kg.
            if col.startswith(PASSTHROUGH_PREFIX):
                continue
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

        # El nombre se PARECE a X pero el contenido dice Y con evidencia propia:
        # gana el contenido. 'Ontvanger' (destinatario, en neerlandes) se parece
        # 0.82 a 'container' y se llevaba la columna de nombres a `packaging`.
        # Un parecido de string entre dos idiomas es la evidencia mas floja que
        # hay; lo que la columna efectivamente contiene es mas fuerte.
        for (col, target), (score, method, why) in list(scored.items()):
            if method != "fuzzy" or content_hits.get((col, target), 0.0) > 0.0:
                continue
            # el rival cuenta solo si por si mismo alcanzaria para asignarse
            rival = max((s for (c, t), s in content_hits.items()
                         if c == col and t != target
                         and s >= cfg.mapping_floor(
                             schema.fields[t].level if t in schema.fields else "delivery")),
                        default=0.0)
            if rival:
                scored[(col, target)] = (
                    score * FUZZY_CONTRADICTED, method,
                    f"{why} pero el contenido de la columna dice otra cosa")

        # el nombre dice X pero el contenido lo contradice fuerte -> bajar
        for (col, target), name_score in name_hits.items():
            if target in ("lat", "lng") and content_hits.get((col, target), 0.0) == 0.0:
                vals = heuristics.ColumnProfile(table.column_values(col, limit=sample_limit))
                score, method, why = scored[(col, target)]
                if vals.nums and not _in_coord_range(target, vals):
                    scored[(col, target)] = (score * 0.55, method,
                                             f"{why} pero los valores NO estan en rango")
                elif vals.nums and (vals.all_int or not vals.has_decimals):
                    # "Nylon"~"lon" con enteros 1..10: no es longitud geografica
                    scored[(col, target)] = (
                        score * 0.35, method,
                        f"{why} pero los valores son enteros (no parecen coordenadas)",
                    )

        result = _assign(scored, table, schema, self.config)
        _resolve_coordinate_pair(result, table, scored)
        _disambiguate_columns(result, table)
        _flag_composite_columns(result, table, sample_limit)
        _flag_review(result, table, self.config)
        return result


def _in_coord_range(target: str, profile) -> bool:
    """Robusto: se tolera hasta un 10% de valores corruptos antes de desconfiar
    del nombre de la columna."""
    lo, hi = (-90.0, 90.0) if target == "lat" else (-180.0, 180.0)
    return profile.mostly_in(lo, hi)


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
        # El piso depende del NIVEL del campo, no es uno solo para todo el
        # schema: reclamar el destino pide mas evidencia que reclamar el peso.
        field = schema.fields.get(target)
        if score < cfg.mapping_floor(field.level if field else "delivery"):
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


def _disambiguate_columns(result: MappingResult, table: Table) -> None:
    """Alias que significan cosas distintas segun el resto de las columnas.

    'Altura' junto a 'Calle' es el numero de puerta; junto a 'Ancho' es el alto
    del bulto. 'Departamento' es una provincia en UY/CO/PE y un depto en AR.
    'Long' es longitud con 'Lat' al lado y largo con 'Ancho'.

    Ninguno se resuelve con alias —el nombre de columna es identico— ni con el
    contenido: 2450 es un numero en los dos casos. Lo resuelve el CONTEXTO, y las
    reglas viven en `resources/vepathos_overrides.json` para que sumar un caso sea
    editar datos.
    """
    reglas = ambiguous_aliases()
    if not reglas:
        return
    for regla in reglas:
        alias = {normalize_key(a) for a in regla.get("alias", ())}
        preferido = regla.get("prefer")
        if not alias or not preferido:
            continue
        columna = next((c for c in table.columns if normalize_key(c) in alias), None)
        if columna is None:
            continue

        presentes = set(result.by_target())
        actual = result.mapping.get(columna)
        if actual is not None and actual.target == preferido:
            continue
        # `when_none` mira el resto: la propia columna no se cuenta como evidencia
        # contra si misma.
        otros = {t for c, m in result.mapping.items() if c != columna for t in (m.target,)}
        if not (set(regla.get("when_any", ())) & presentes):
            continue
        if set(regla.get("when_none", ())) & otros:
            continue

        anterior = actual.target if actual else "(sin mapear)"
        if actual is not None and preferido in otros:
            continue                     # ya hay otra columna en ese target
        if actual is None:
            result.mapping[columna] = ColumnMapping(
                columna, preferido, 0.80, "heuristic", "")
            if columna in result.unmapped:
                result.unmapped.remove(columna)
            actual = result.mapping[columna]
        actual.target = preferido
        actual.evidence += (f" | '{columna}' es ambiguo; el contexto de columnas "
                            f"indica {preferido}")
        result.warnings.append(
            f"'{columna}' se mapeo a {preferido} (no a {anterior}) por el contexto "
            "del archivo. Si en el tuyo significa otra cosa, corregilo en el mapping.")



def _flag_composite_columns(result: MappingResult, table: Table, limit: int) -> None:
    """Una columna que mezcla varios campos ('Juan Perez - Corrientes 1250 - tel 11...').

    Las reglas la mapean entera a `address`, que es lo mejor que pueden hacer, pero
    el resultado arrastra nombre y telefono adentro de la direccion y eso rompe el
    geocoding. Marcarla baja la confianza Y le dice al pipeline que corra el
    `FieldExtractionPipeline` SOLO sobre esta columna: el resto del archivo sigue
    el camino rapido.
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
                f"'{col}' parece contener varios campos en un solo texto. Se separa "
                "en nombre/direccion/telefono con reglas durante el normalize "
                "(sin modelo)."
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
