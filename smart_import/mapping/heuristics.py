"""Paso 4: deteccion por CONTENIDO de la columna, no por su nombre.

Es lo que salva los archivos con headers inutiles ('col_3', 'Campo 1', 'A').
Los scores estan topeados por debajo de un alias exacto: el nombre gana si existe,
el contenido decide cuando el nombre no dice nada o miente.
"""
from __future__ import annotations

import re
from typing import Any

CAP = 0.93                      # techo general: nunca le gana a un alias exacto
CAP_STRONG = 0.95               # solo para evidencia dura (rango de coordenadas)

_PHONE_RE = re.compile(r"^[+()\d][\d\s\-().]{5,}$")
_TZ_RE = re.compile(r"^[A-Za-z]+/[A-Za-z_+\-0-9]+$|^(UTC|GMT)([+-]\d{1,2})?$", re.I)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}")
_ID_RE = re.compile(r"^[A-Za-z0-9]+([-_][A-Za-z0-9]+)*$")
_PACKAGING = {"box", "pallet", "bag", "envelope", "caja", "pallets", "sobre", "bolsa", "crate"}
_STATUS = {"pending", "delivered", "failed", "in_transit", "pendiente", "entregado",
           "cancelado", "cancelled", "new", "assigned", "done"}


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
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
        return float(s)
    except ValueError:
        return None


def _is_phone(text: str) -> bool:
    digits = sum(c.isdigit() for c in text)
    if not (7 <= digits <= 15):
        return False
    if not _PHONE_RE.match(text):
        return False
    return "." not in text                      # 0.5 no es un telefono


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
        self.has_decimals = any(abs(x - int(x)) > 1e-9 for x in self.nums)
        self.all_int = bool(self.nums) and not self.has_decimals
        self.lo = min(self.nums) if self.nums else None
        self.hi = max(self.nums) if self.nums else None

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
            add("lng", 0.84, f"en [-180,180] con >=3 decimales")

        if p.all_int and 0 < p.lo and p.hi <= 200 and p.distinct <= 30:
            add("quantity", 0.72, f"enteros chicos {int(p.lo)}..{int(p.hi)}")
        if p.all_int and 1 <= p.lo and p.hi <= 10 and p.distinct <= 10:
            add("priority", 0.70, f"enteros 1..10, {p.distinct} valores distintos")
        if p.has_decimals and 0 <= p.lo and p.hi <= 2000:
            add("weight_kg", 0.62, f"decimales en 0..2000 ({p.lo:g}..{p.hi:g})")
        if 0 <= p.lo and p.hi <= 480 and p.distinct <= 40:
            add("service_time_min", 0.55, f"0..480, {p.distinct} distintos")
        if p.all_int and p.lo >= 0 and p.hi > 1000:
            add("value_cents", 0.55, f"enteros grandes hasta {int(p.hi)}")

    # --- textuales ---
    if p.numeric_frac < 0.5:
        if p.frac(lambda t: bool(_TZ_RE.match(t))) >= 0.8:
            add("tw_timezone", 0.92, "formato IANA/UTC (Region/Ciudad)")
        if p.frac(lambda t: bool(_DATE_RE.search(t))) >= 0.8:
            add("tw_start", 0.55, "parseable como fecha/hora")
            add("tw_end", 0.55, "parseable como fecha/hora")

        has_num_and_word = p.frac(lambda t: any(c.isdigit() for c in t) and any(c.isalpha() for c in t))
        if p.avg_len >= 12 and has_num_and_word >= 0.6 and p.cardinality >= 0.4:
            add("address", 0.84, f"texto largo ({p.avg_len:.0f} chars) con numero y palabras")

        idish = p.frac(_is_identifier)
        if idish >= 0.8 and p.avg_len <= 32:
            if p.cardinality >= 0.95:
                add("package_id", 0.78, f"identificador casi unico ({p.cardinality:.0%} distintos)")
            elif p.cardinality >= 0.3:
                add("delivery_id", 0.76, f"identificador con repeticiones ({p.distinct} grupos)")

        if p.distinct <= 12 and p.avg_len <= 24 and (p.cardinality < 0.35 or p.distinct <= 8):
            lowers = {t.lower() for t in p.texts}
            if lowers & _STATUS:
                add("status", 0.86, f"valores de estado conocidos: {sorted(lowers)[:3]}")
            elif lowers & _PACKAGING:
                add("packaging", 0.86, f"tipos de embalaje: {sorted(lowers)[:3]}")
            else:
                add("zone", 0.66, f"baja cardinalidad ({p.distinct} valores)")

        words = p.frac(lambda t: not any(c.isdigit() for c in t) and 2 <= len(t.split()) <= 5)
        if words >= 0.85 and 6 <= p.avg_len <= 45 and p.cardinality >= 0.5:
            add("customer_name", 0.74, "texto alfabetico de 2-5 palabras, sin digitos")

    return out
