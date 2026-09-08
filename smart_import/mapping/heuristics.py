"""Paso 4: deteccion por CONTENIDO de la columna, no por su nombre.

Es lo que salva los archivos con headers inutiles ('col_3', 'Campo 1', 'A').
Los scores estan topeados por debajo de un alias exacto: el nombre gana si existe,
el contenido decide cuando el nombre no dice nada o miente.

Vocabulario de empaque/estado: VocabularyStore (catálogo → SQLite).
Codigos UNECE de 2–3 letras (BX, BA, RO) solo matchean exactos en columnas
que parecen codigos categoricos — nunca fuzzy.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache
from typing import Any

from ..resources import fold, packaging_codes, packaging_words, status_words

CAP = 0.93                      # techo general: nunca le gana a un alias exacto
CAP_STRONG = 0.95               # solo para evidencia dura (rango de coordenadas)

#: Evidencia promedio para declarar que una columna ES la direccion. El margen
#: es comodo a proposito: las direcciones dan 0.70+, lo demas no pasa de 0.20.
ADDRESS_MIN_MEAN = 0.50

#: Estos scores dicen CUANTA EVIDENCIA HAY, no si alcanza. El piso lo pone
#: `Config.mapping_floor(level)` y depende del nivel del campo: reclamar el
#: destino (`delivery`) pide mas que reclamar un bulto (`package`). Al escribir
#: una regla nueva hay que estimar la evidencia con honestidad y dejar que el
#: piso decida — subir el score para "pasar el corte" es mentirle al reporte,
#: que es donde el operador lee por que el sistema creyo lo que creyo.

_PHONE_RE = re.compile(r"^[+()\d][\d\s\-().]{5,}$")
_TZ_RE = re.compile(r"^[A-Za-z]+/[A-Za-z_+\-0-9]+$|^(UTC|GMT)([+-]\d{1,2})?$", re.I)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}")
_ID_RE = re.compile(r"^[A-Za-z0-9]+([-_][A-Za-z0-9]+)*$")


def _is_monotonic_ids(nums: list[float]) -> bool:
    """True si parece nro de orden (1,2,3… o 1001,1002…), no alturas de calle."""
    if len(nums) < 3:
        return False
    ordered = sorted(int(x) for x in nums)
    diffs = [b - a for a, b in zip(ordered, ordered[1:])]
    return bool(diffs) and all(d == 1 for d in diffs)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace(" ", "")
    # coma decimal (0,5) vs separador de miles (1,234.5)
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) != 3 else s.replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return None
    # JSON no-estándar: NaN / Infinity (común en dumps de Python / pandas)
    return f if math.isfinite(f) else None


#: 'Mobile: 1199887766' — el export trae la etiqueta adentro de cada celda.
#: Pasa en cualquier contacto exportado de Outlook/Google y en las planillas que
#: alguien "arregló" a mano. La etiqueta no cambia lo que el valor ES.
_CELL_LABEL = re.compile(r"^[^\d+(]{1,24}[:\-]\s*")


def _strip_cell_label(text: str) -> str:
    return _CELL_LABEL.sub("", text, count=1).strip()


def _is_phone(text: str) -> bool:
    candidate = text if text[:1].isdigit() or text[:1] in "+(" else _strip_cell_label(text)
    digits = sum(c.isdigit() for c in candidate)
    if not (7 <= digits <= 15):
        return False
    if not _PHONE_RE.match(candidate):
        return False
    return "." not in candidate                 # 0.5 no es un telefono


@lru_cache(maxsize=1)
def _address_scorer():
    """El MISMO scorer que usan el geocoder, el extractor y el clasificador.

    Antes esto era un regex de vias en castellano embebido acá. Duplicar la
    lista es como se desincronizan dos modulos que creen tener la misma regla:
    el extractor aprendia 'Hauptstrasse' y el mapper seguia sin entenderla.
    """
    from ..addresses.scoring import AddressCandidateScorer
    return AddressCandidateScorer()


@lru_cache(maxsize=1)
def _street_words() -> frozenset[str]:
    from ..resources import label_set
    return label_set("street_tokens", None) | label_set("street_suffixes", None)


def _has_street_token(text: str) -> bool:
    """Tipo de via suelto ('Av. Corrientes') o pegado ('Hauptstrasse')."""
    words = _street_words()
    for raw in text.split():
        token = fold(raw.strip(".,;:"))
        if token in words:
            return True
        if len(token) > 4 and any(token.endswith(w) for w in words if len(w) >= 3):
            return True
    return False


def _address_evidence(p: "ColumnProfile", sample: int = 40) -> float:
    """Evidencia promedio de direccion de la columna, 0..0.99.

    Una direccion real puntua 0.70-0.95; un nombre, un telefono o una zona
    puntuan 0.00-0.20. El margen entre los dos grupos es lo que permite decidir
    sin mirar el nombre de la columna, que es justo lo que hace falta cuando el
    header viene en un idioma que el schema no lista.
    """
    texts = [t for t in p.texts[:sample] if len(t) >= 6]
    if not texts:
        return 0.0
    scorer = _address_scorer()
    return sum(scorer.score(t).score for t in texts) / len(texts)


def _weight_unit_frac(p: "ColumnProfile", sample: int = 40) -> float:
    """Fraccion de celdas que traen la unidad de peso escrita: '2,75 kg', '10 lb'.

    Es la unica señal de peso que cruza idiomas sin depender del header: `kg`,
    `lb`, `grs` y `quilos` se escriben igual en el archivo aleman y en el
    brasileño. Sale del mismo lexico de paqueteria que lee el texto libre.
    """
    texts = [t for t in p.texts[:sample] if t]
    if not texts:
        return 0.0
    from ..packages import get_package_lexicon
    pattern = get_package_lexicon().weight_re
    hits = 0
    for text in texts:
        match = pattern.search(text)
        # el patron acepta la unidad sola ('kg'); acá hace falta el numero
        if match and (match.group("n") or match.group("nword")):
            hits += 1
    return hits / len(texts)


def _is_identifier(text: str) -> bool:
    if not _ID_RE.match(text) or " " in text:
        return False
    has_digit = any(c.isdigit() for c in text)
    has_alpha = any(c.isalpha() for c in text)
    return has_digit and (has_alpha or len(text) >= 6)


class ColumnProfile:
    """Estadisticas de una columna, calculadas una sola vez."""

    def __init__(self, values: list[Any]):
        self.raw = [v for v in values if v is not None and str(v).strip() != ""]
        self.n = len(self.raw)
        self.texts = [str(v).strip() for v in self.raw]
        nums = [_to_float(v) for v in self.raw]
        self.nums = [n for n in nums if n is not None]
        self.numeric_frac = (len(self.nums) / self.n) if self.n else 0.0
        self.distinct = len(set(self.texts))
        self.cardinality = (self.distinct / self.n) if self.n else 0.0
        self.avg_len = (sum(len(t) for t in self.texts) / self.n) if self.n else 0.0
        # int(NaN) / int(inf) explotan; nums ya filtra no-finitos via _to_float
        self.has_decimals = any(
            abs(x - int(x)) > 1e-9 for x in self.nums if math.isfinite(x)
        )
        self.all_int = bool(self.nums) and not self.has_decimals
        finite = [x for x in self.nums if math.isfinite(x)]
        self.lo = min(finite) if finite else None
        self.hi = max(finite) if finite else None

    def frac(self, pred) -> float:
        return (sum(1 for t in self.texts if pred(t)) / self.n) if self.n else 0.0

    def frac_in(self, lo: float, hi: float) -> float:
        """Fraccion de valores numericos dentro del rango (tolerante a outliers)."""
        if not self.nums:
            return 0.0
        return sum(1 for x in self.nums if lo <= x <= hi) / len(self.nums)

    def mostly_in(self, lo: float, hi: float) -> bool:
        """True si los valores caen en el rango salvo unos pocos outliers.

        Un porcentaje fijo no sirve: en una muestra de 3 valores UNA fila corrupta
        ya es el 33% y daria vuelta la clasificacion de la columna entera (lat
        pasaria a leerse como lng). Se permite 1 outlier siempre, o el 10% cuando
        la muestra es grande.
        """
        if not self.nums:
            return False
        outliers = sum(1 for x in self.nums if not (lo <= x <= hi))
        return outliers <= max(1, int(len(self.nums) * 0.1))

    def trimmed_spread(self) -> float:
        """Amplitud entre los percentiles 5 y 95: ignora valores corruptos sueltos."""
        if not self.nums:
            return 0.0
        ordered = sorted(self.nums)
        n = len(ordered)
        lo = ordered[max(0, int(n * 0.05))]
        hi = ordered[min(n - 1, int(n * 0.95))]
        return hi - lo


def candidates(values: list[Any], profile: ColumnProfile | None = None
               ) -> list[tuple[str, float, str, str]]:
    """[(target, confidence, 'heuristic', evidencia)] segun el contenido."""
    p = profile or ColumnProfile(values)
    if p.n == 0:
        return []
    out: list[tuple[str, float, str, str]] = []

    def add(target: str, score: float, why: str, cap: float = CAP) -> None:
        if score > 0:
            out.append((target, min(cap, score), "heuristic", why))

    # --- telefono: se evalua primero porque '1155554444' PARSEA como numero
    #     y quedaria atrapado en la rama numerica sin llegar nunca a la textual ---
    phoneish = p.frac(_is_phone)
    if phoneish >= 0.7:
        add("phone", 0.88, f"{phoneish:.0%} con patron telefonico (7-15 digitos)")

    # --- numericas ---
    if p.numeric_frac >= 0.9 and p.nums and phoneish < 0.7:
        # Rangos ROBUSTOS: una sola fila corrupta (lat=95) no puede reclasificar la
        # columna entera. Esa fila se reporta despues como invalida, en normalize.
        in_lat = p.mostly_in(-90, 90)
        in_lng = p.mostly_in(-180, 180)
        frac_lat = p.frac_in(-90, 90)
        spread = p.trimmed_spread()
        precise = p.has_decimals and any(len(str(abs(x)).split(".")[-1]) >= 3 for x in p.nums)

        if precise and in_lat and spread < 40:
            add("lat", 0.86, f"{frac_lat:.0%} en [-90,90] con >=3 decimales")
        if precise and in_lng and not in_lat:
            # una parte sustancial excede |90| => imposible que sea latitud
            add("lng", 0.95, f"{1 - frac_lat:.0%} de los valores fuera de [-90,90]", CAP_STRONG)
        elif precise and in_lng and spread < 40:
            add("lng", 0.84, "en [-180,180] con >=3 decimales")

        if p.all_int and 0 < p.lo and p.hi <= 200 and p.distinct <= 30:
            add("quantity", 0.72, f"enteros chicos {int(p.lo)}..{int(p.hi)}")
        # Altura tipica: enteros/cortos, NO secuencia de orden (1,2,3… o 1001,1002…).
        if (
            p.numeric_frac >= 0.9
            and p.all_int                    # 2.75 no es la altura de una calle
            and p.avg_len <= 6
            and p.hi <= 30_000
            and p.cardinality >= 0.5
            and not _is_monotonic_ids(p.nums)
        ):
            add("house_number", 0.70,
                f"numeros cortos no-secuenciales ({int(p.lo)}..{int(p.hi)})")
        if p.all_int and 1 <= p.lo and p.hi <= 10 and p.distinct <= 10:
            add("priority", 0.70, f"enteros 1..10, {p.distinct} valores distintos")
        if p.has_decimals and 0 <= p.lo and p.hi <= 2000:
            # Decimales chicos y variados: la forma de una columna de peso. Un
            # importe suele ser mas grande o entero y una duracion suele
            # repetirse, pero ninguna de las tres es distinguible con certeza.
            # El score dice cuanta evidencia HAY; que alcance o no para
            # asignarse lo decide el piso del nivel `package`, no este numero.
            if p.hi <= 500 and p.cardinality >= 0.5 and not _is_monotonic_ids(p.nums):
                add("weight_kg", 0.66,
                    f"decimales chicos y variados ({p.lo:g}..{p.hi:g}, "
                    f"{p.cardinality:.0%} distintos)")
            else:
                add("weight_kg", 0.62, f"decimales en 0..2000 ({p.lo:g}..{p.hi:g})")
        if 0 <= p.lo and p.hi <= 480 and p.distinct <= 40:
            add("service_time_min", 0.55, f"0..480, {p.distinct} distintos")
        if p.all_int and p.lo >= 0 and p.hi > 1000:
            add("value_cents", 0.55, f"enteros grandes hasta {int(p.hi)}")

    # La unidad escrita en la celda gana sobre cualquier heuristica de forma, y
    # no depende del idioma del header: '2,75 kg' dice lo que es en cualquier
    # archivo. Va antes que lo textual porque '10 lb' tambien parece texto.
    unit_frac = _weight_unit_frac(p)
    if unit_frac >= 0.6:
        add("weight_kg", 0.90, f"{unit_frac:.0%} de las celdas traen unidad de peso")

    # --- textuales ---
    if p.numeric_frac < 0.5:
        if p.frac(lambda t: bool(_TZ_RE.match(t))) >= 0.8:
            add("tw_timezone", 0.92, "formato IANA/UTC (Region/Ciudad)")
        if p.frac(lambda t: bool(_DATE_RE.search(t))) >= 0.8:
            add("tw_start", 0.55, "parseable como fecha/hora")
            add("tw_end", 0.55, "parseable como fecha/hora")

        # Intersecciones tipicas AR/LatAm: "Alsina y Pelegrini", "Maipu e Yrigoyen"
        has_intersection = p.frac(
            lambda t: bool(re.search(r"\s+[ye]\s+", t, re.I)) and len(t.split()) >= 3
        )
        has_street_token = p.frac(_has_street_token)
        address_mean = _address_evidence(p)
        if address_mean >= ADDRESS_MIN_MEAN and p.cardinality >= 0.4:
            add("address", 0.55 + address_mean * 0.45,
                f"evidencia de direccion {address_mean:.2f} promedio "
                f"(mismo scorer que usa el geocoder)")
        elif (
            p.avg_len >= 10
            and p.cardinality >= 0.5
            and p.numeric_frac < 0.25
            and (has_intersection >= 0.12 or has_street_token >= 0.2)
        ):
            # Planillas de reparto local: muchas calles SIN altura ("Alsina y
            # Pelegrini"). El scorer las puntua bajo a proposito —no son una
            # direccion precisa— pero como columna siguen siendo el destino.
            add(
                "address",
                0.80,
                f"texto de lugar sin altura ({p.avg_len:.0f} chars; "
                f"interseccion={has_intersection:.0%} via={has_street_token:.0%})",
            )

        idish = p.frac(_is_identifier)
        if idish >= 0.8 and p.avg_len <= 32:
            if p.cardinality >= 0.95:
                add("package_id", 0.78, f"identificador casi unico ({p.cardinality:.0%} distintos)")
            elif p.cardinality >= 0.3:
                add("delivery_id", 0.76, f"identificador con repeticiones ({p.distinct} grupos)")

        if p.distinct <= 12 and p.avg_len <= 24 and (p.cardinality < 0.35 or p.distinct <= 8):
            lowers = {t.lower() for t in p.texts}
            folded = {fold(t) for t in p.texts}
            codes = packaging_codes()
            words = packaging_words()
            word_hits = (lowers | folded) & (words - codes)
            code_hits = folded & codes
            looks_like_codes = p.avg_len <= 4 and p.frac(
                lambda t: 2 <= len(t.strip()) <= 3 and t.strip().isalpha()
            ) >= 0.6
            if lowers & status_words() or folded & status_words():
                add("status", 0.86, f"valores de estado conocidos: {sorted(lowers)[:3]}")
            elif word_hits or (looks_like_codes and code_hits):
                add("packaging", 0.86, f"tipos de embalaje: {sorted(lowers)[:3]}")
            else:
                add("zone", 0.66, f"baja cardinalidad ({p.distinct} valores)")

        words = p.frac(lambda t: not any(c.isdigit() for c in t) and 2 <= len(t.split()) <= 5)
        if words >= 0.85 and 6 <= p.avg_len <= 45 and p.cardinality >= 0.5:
            add("customer_name", 0.74, "texto alfabetico de 2-5 palabras, sin digitos")

    return out
