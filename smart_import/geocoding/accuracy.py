"""Cuanto acierta el geocoder, medido contra coordenadas verificadas.

Es el instrumento que decide si esto se puede ofrecer como servicio. Responde una
sola pregunta con un numero: de N direcciones con coordenada conocida, ¿a cuantos
metros cae nuestro pin?

Dos decisiones de medicion que cambian el resultado y por eso son explicitas:

  * **Se separa por altura.** Una direccion sin numero de puerta ('Av Victorica')
    no se puede resolver a puerta ni en teoria. Promediarlas con las demas
    esconde el problema real; el reporte las cuenta aparte.
  * **El indice se elige por el bbox de los datos.** Medir con el indice
    equivocado da 10 km de error mediano y parece un problema de scoring cuando
    es de cobertura.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..normalization.address import house_number_already_in_street
from ..normalization.values import to_float
from ..readers import read_any
from ..schemas import normalize_key
from .bands import BAND_NEEDS_GEOCODING, BAND_VALID, DEFAULT_VALID_AT, band_for
from .coord_check import usable_truth_coord
from .osm_geocoder import LocalOSMGeocoder
from .scoring import haversine_km

#: alias exactos por campo; si no matchean se cae a la busqueda por substring
COLUMN_ALIASES = {
    "address": ("address", "address line", "addressline", "direccion", "domicilio",
                "street", "full address", "calle", "destino"),
    "lat": ("lat", "latitude", "latitud", "y", "coord y"),
    "lng": ("lng", "lon", "long", "longitude", "longitud", "x", "coord x"),
}
#: fragmentos que delatan la columna aunque venga con prefijos del cliente
#: ('receiver_address_latitude', 'shipping_address_line')
COLUMN_HINTS = {
    "address": ("address", "direccion", "domicilio", "street", "calle"),
    "lat": ("latitude", "latitud", "_lat", "lat_"),
    "lng": ("longitude", "longitud", "_lng", "_lon", "lng_", "lon_"),
}

#: una altura es 1-5 digitos sueltos; '03845' y '1358' cuentan, '400069' no
_HOUSE_NUMBER = re.compile(r"(?<!\d)\d{1,5}(?!\d)")

#: distancias de corte del reporte, en metros
BUCKETS = (100, 250, 500, 1000)
#: umbral oficial de "acierto" (pin publicado a no más de esto)
HIT_M = 100
#: pin 'valid' mas lejos que esto = falso positivo (el operador no lo revisa)
OVERCONFIDENT_M = 250
#: ancho máximo de address en la tabla (terminal)
_ADDR_W = 42
#: precision compacta en el dump
_PREC_SHORT = {
    "housenumber": "hn",
    "street": "st",
    "street_mismatch": "mis",
    "street_weak": "weak",
    "locality": "loc",
    "poi": "poi",
    "suspect": "sus",
    "below_threshold": "low",
}


def find_house_column(columns: list[str]) -> str | None:
    """Altura en columna aparte (shipping_address.number), no el número de orden."""
    normalizadas = {c: normalize_key(c) for c in columns}
    for columna, clave in normalizadas.items():
        plano = clave.replace(" ", "_")
        if "address" in plano and "number" in plano:
            return columna
    for alias in ("house_number", "housenumber", "street_number", "altura",
                  "numero"):
        for columna, clave in normalizadas.items():
            if clave == alias:
                return columna
    return None


def compose_street_and_number(street: str, number: str | None) -> str:
    """'Av. Rivadavia' + '4800' → 'Av. Rivadavia 4800' si la altura no está."""
    calle = (street or "").strip()
    altura = str(number or "").strip()
    if not altura or not calle:
        return calle
    if house_number_already_in_street(calle, altura):
        return calle
    return f"{calle} {altura}".strip()


def find_column(columns: list[str], field: str) -> str | None:
    """La columna de `columns` que corresponde a `field`, o None.

    Primero alias exactos (normalizados), despues substring: los exports reales
    traen 'receiver_address_latitude', no 'lat'.
    """
    normalizadas = {c: normalize_key(c) for c in columns}
    for columna, clave in normalizadas.items():
        if clave in COLUMN_ALIASES[field]:
            return columna
    for columna, clave in normalizadas.items():
        plano = clave.replace(" ", "_")
        if any(h in plano for h in COLUMN_HINTS[field]):
            return columna
    return None


def has_house_number(address: str) -> bool:
    return bool(_HOUSE_NUMBER.search(address or ""))


@dataclass
class Group:
    """Un subconjunto de la muestra (con altura / sin altura)."""
    name: str
    total: int = 0
    with_pin: int = 0
    errors_m: list[float] = field(default_factory=list)
    by_band: dict[str, int] = field(default_factory=dict)
    #: error por banda, para saber si 'valid' de verdad significa valid
    errors_by_band: dict[str, list[float]] = field(default_factory=dict)
    confidences: list[float] = field(default_factory=list)
    confidences_pin: list[float] = field(default_factory=list)

    def add(self, band: str, error_m: float | None,
            confidence: float | None = None) -> None:
        self.total += 1
        self.by_band[band] = self.by_band.get(band, 0) + 1
        if confidence is not None:
            self.confidences.append(confidence)
        if error_m is None:
            return
        self.with_pin += 1
        self.errors_m.append(error_m)
        self.errors_by_band.setdefault(band, []).append(error_m)
        if confidence is not None:
            self.confidences_pin.append(confidence)

    def within(self, metros: float) -> int:
        return sum(1 for e in self.errors_m if e <= metros)

    def as_dict(self) -> dict:
        errores = sorted(self.errors_m)
        salida = {
            "group": self.name,
            "total": self.total,
            "with_pin": self.with_pin,
            "pin_pct": _pct(self.with_pin, self.total),
            "median_m": round(statistics.median(errores), 1) if errores else None,
            "p90_m": (round(errores[int(len(errores) * 0.9) - 1], 1)
                      if len(errores) > 1 else None),
            "max_m": round(errores[-1], 1) if errores else None,
            "mean_confidence": _mean(self.confidences),
            "mean_confidence_pin": _mean(self.confidences_pin),
            "hit_pct": _pct(self.within(HIT_M), self.total),
            "bands": dict(self.by_band),
        }
        for metros in BUCKETS:
            n = self.within(metros)
            salida[f"within_{metros}m"] = n
            # sobre el TOTAL, no sobre los que tienen pin: un servicio se juzga
            # por lo que resuelve de lo que le mandaste, no de lo que intento.
            salida[f"within_{metros}m_pct"] = _pct(n, self.total)
        salida["band_quality"] = {
            banda: {"n": len(v), "median_m": round(statistics.median(v), 1)}
            for banda, v in sorted(self.errors_by_band.items())
        }
        return salida


def _pct(parte: int, total: int) -> float:
    return round(100.0 * parte / total, 1) if total else 0.0


def _mean(valores: list[float]) -> float | None:
    return round(statistics.mean(valores), 3) if valores else None


@dataclass
class AccuracyReport:
    source: str = ""
    index: str = ""
    rows_read: int = 0
    rows_usable: int = 0
    overall: Group = field(default_factory=lambda: Group("todas"))
    with_number: Group = field(default_factory=lambda: Group("con altura"))
    without_number: Group = field(default_factory=lambda: Group("sin altura"))
    worst: list[dict] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    depot_tokens: list[str] = field(default_factory=list)
    depot_warning: str | None = None
    libpostal: dict = field(default_factory=dict)
    regions: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        out = {
            "source": self.source, "index": self.index,
            "rows_read": self.rows_read, "rows_usable": self.rows_usable,
            "depot_tokens": list(self.depot_tokens),
            "depot_warning": self.depot_warning,
            "libpostal": dict(self.libpostal),
            "calibration": calibration(self.rows),
            "overall": self.overall.as_dict(),
            "with_house_number": self.with_number.as_dict(),
            "without_house_number": self.without_number.as_dict(),
            "worst": sorted(self.worst, key=lambda w: -w["error_m"])[:15],
        }
        if self.regions:
            out["regions"] = list(self.regions)
        return out


def load_truth(path: str | Path) -> tuple[list[dict], dict[str, str]]:
    """(filas, columnas detectadas) desde CSV, JSON o XLSX.

    Reusa `read_any`, asi que cualquier formato que el import ya acepta sirve
    como corpus sin preprocesarlo.
    """
    tabla = read_any(path)
    columnas = {campo: find_column(tabla.columns, campo)
                for campo in ("address", "lat", "lng")}
    faltan = [k for k, v in columnas.items() if v is None]
    if faltan:
        raise ValueError(
            f"no encuentro la(s) columna(s) {faltan} en {Path(path).name}. "
            f"Columnas disponibles: {tabla.columns}")
    house_col = find_house_column(tabla.columns)
    if house_col:
        columnas["house"] = house_col
    filas = list(tabla.iter_dicts())
    if house_col:
        addr_col = columnas["address"]
        for fila in filas:
            fila[addr_col] = compose_street_and_number(
                str(fila.get(addr_col) or ""), fila.get(house_col))
    return filas, columnas


def data_bbox(filas: list[dict], columnas: dict[str, str]
              ) -> tuple[float, float, float, float] | None:
    """(north, south, east, west) de las coordenadas verdaderas."""
    lats, lons = [], []
    for fila in filas:
        la, lo = to_float(fila.get(columnas["lat"])), to_float(fila.get(columnas["lng"]))
        if not usable_truth_coord(la, lo):
            continue
        lats.append(la)
        lons.append(lo)
    if not lats:
        return None
    return (max(lats), min(lats), max(lons), min(lons))


@dataclass(frozen=True)
class OriginChoice:
    """Origen del depot para geocode-accuracy."""

    lat: float
    lon: float
    source: str
    address: str = ""
    warning: str | None = None


def first_truth_origin(filas: list[dict], columnas: dict[str, str]
                       ) -> OriginChoice | None:
    """Lat/lng (y address) de la primera fila usable del corpus."""
    addr_col, lat_col, lng_col = columnas["address"], columnas["lat"], columnas["lng"]
    for fila in filas:
        lat = to_float(fila.get(lat_col))
        lon = to_float(fila.get(lng_col))
        if not usable_truth_coord(lat, lon):
            continue
        address = str(fila.get(addr_col) or "").strip()
        return OriginChoice(lat=lat, lon=lon, source="first-address",
                            address=address)
    return None


def resolve_accuracy_origin(
    filas: list[dict],
    columnas: dict[str, str],
    *,
    origin_lat: float | None = None,
    origin_lon: float | None = None,
    max_distance_km: float = 500.0,
) -> OriginChoice:
    """Origen del depot: 1er address, salvo que --origin-lat/lon estén cerca.

    Si el flag queda más lejos que `max_distance_km` del corpus, se ignora
    (el caso típico: coords de otra ciudad pegadas al corpus local).
    """
    fallback = first_truth_origin(filas, columnas)
    if fallback is None:
        raise ValueError("el archivo no tiene ninguna coordenada valida")

    if (origin_lat is None) != (origin_lon is None):
        raise ValueError("pasa --origin-lat y --origin-lon juntos, o ninguno")

    if origin_lat is None:
        return fallback

    far_km = haversine_km(origin_lat, origin_lon, fallback.lat, fallback.lon)
    if far_km > float(max_distance_km):
        return OriginChoice(
            lat=fallback.lat, lon=fallback.lon, source="first-address",
            address=fallback.address,
            warning=(
                f"--origin-lat/lon {origin_lat:.4f},{origin_lon:.4f} queda a "
                f"{far_km:.0f} km del corpus; uso el 1er address"
            ),
        )
    return OriginChoice(
        lat=float(origin_lat), lon=float(origin_lon), source="flag",
        address=fallback.address,
    )


def group_truth_rows(filas: list[dict]) -> list[tuple[str, list[dict]]]:
    """Agrupa un mix por `preset` o, si falta, `country`. Orden de primera aparición."""
    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    for fila in filas:
        key = (
            str(fila.get("preset") or "").strip()
            or str(fila.get("country") or "").strip()
            or "_unknown"
        )
        if key not in buckets:
            order.append(key)
            buckets[key] = []
        buckets[key].append(fila)
    return [(key, buckets[key]) for key in order]


def is_multi_region_truth(filas: list[dict]) -> bool:
    """True si el corpus mezcla 2+ países/presets (un PBF mundial no existe)."""
    keys = {k for k, _ in group_truth_rows(filas) if k != "_unknown"}
    return len(keys) >= 2


def _merge_group(into: Group, src: Group) -> None:
    into.total += src.total
    into.with_pin += src.with_pin
    into.errors_m.extend(src.errors_m)
    into.confidences.extend(src.confidences)
    into.confidences_pin.extend(src.confidences_pin)
    for banda, n in src.by_band.items():
        into.by_band[banda] = into.by_band.get(banda, 0) + n
    for banda, errs in src.errors_by_band.items():
        into.errors_by_band.setdefault(banda, []).extend(errs)


def _merge_libpostal(stats: list[dict]) -> dict:
    if not stats:
        return {}
    out = dict(stats[0])
    reasons: dict[str, int] = dict(out.get("by_reason") or {})
    for extra in stats[1:]:
        for key in ("enhancer_calls", "enhancer_skips", "helped", "noop"):
            if key in extra or key in out:
                out[key] = int(out.get(key) or 0) + int(extra.get(key) or 0)
        for key, n in (extra.get("by_reason") or {}).items():
            reasons[key] = reasons.get(key, 0) + int(n)
    if reasons:
        out["by_reason"] = reasons
    return out


def merge_accuracy_reports(
    source: str, reports: list[AccuracyReport],
) -> AccuracyReport:
    """Junta corridas por país en un reporte (dump / gate / totales)."""
    merged = AccuracyReport(source=source, index="(multi-region)")
    for reporte in reports:
        merged.rows_read += reporte.rows_read
        merged.rows_usable += reporte.rows_usable
        merged.rows.extend(reporte.rows)
        merged.worst.extend(reporte.worst)
        _merge_group(merged.overall, reporte.overall)
        _merge_group(merged.with_number, reporte.with_number)
        _merge_group(merged.without_number, reporte.without_number)
    merged.libpostal = _merge_libpostal([r.libpostal for r in reports if r.libpostal])
    return merged


def compact_region_summary(key: str, reporte: AccuracyReport) -> str:
    """Una línea por país: n, acierto ≤100 m, mediana."""
    grupo = reporte.overall
    hit = _pct(grupo.within(HIT_M), grupo.total)
    mediana = (
        f"{round(statistics.median(grupo.errors_m)):.0f} m"
        if grupo.errors_m else "—"
    )
    return f"  {key:<22} n={grupo.total:<5} ≤100m {hit:5.1f}%  mediana {mediana}"


@dataclass
class RegionOutcome:
    """Resultado de un país en un mix (o skip si no hubo índice)."""

    key: str
    n_input: int
    report: AccuracyReport | None = None
    skip: str | None = None
    index_name: str = ""

    def as_dict(self) -> dict:
        if self.skip or self.report is None:
            return {
                "region": self.key, "n": self.n_input, "skip": self.skip or "sin reporte",
            }
        d = self.report.overall.as_dict()
        return {
            "region": self.key,
            "n": d["total"],
            "pin_pct": d["pin_pct"],
            "within_100m_pct": d["within_100m_pct"],
            "median_m": d["median_m"],
            "index": self.index_name,
        }


def format_regions_table(outcomes: list[RegionOutcome]) -> str:
    """Tabla por país al final del mix (no se pierde entre logs de índice)."""
    header = (f"{'país':22} {'n':>5} {'pin':>7} {'≤100m':>7} {'mediana':>10}  nota")
    lines = [
        "por país (después de elegir índice; SKIP = sin PBF / extract vacío):",
        header,
        "-" * len(header),
    ]
    measured = [o for o in outcomes if o.report is not None]
    skipped = [o for o in outcomes if o.report is None]
    measured.sort(key=lambda o: (
        -o.report.overall.within(HIT_M) / o.report.overall.total
        if o.report and o.report.overall.total else 0.0,
        o.key,
    ))
    for outcome in measured + skipped:
        if outcome.skip or outcome.report is None:
            reason = (outcome.skip or "sin reporte").split(";")[0]
            if len(reason) > 56:
                reason = reason[:53] + "…"
            lines.append(
                f"{outcome.key:22} {outcome.n_input:>5} {'—':>7} {'—':>7} {'—':>10}  "
                f"SKIP {reason}")
            continue
        d = outcome.report.overall.as_dict()
        mediana = f"{d['median_m']:.0f} m" if d["median_m"] is not None else "—"
        lines.append(
            f"{outcome.key:22} {d['total']:>5} {d['pin_pct']:>6.0f}% "
            f"{d['within_100m_pct']:>6.0f}% {mediana:>10}  {outcome.index_name}")
    n_ok = sum(o.report.overall.total for o in measured if o.report)
    n_skip = sum(o.n_input for o in skipped)
    lines.append("")
    lines.append(
        f"{len(measured)} países medidos ({n_ok} filas)  ·  "
        f"{len(skipped)} omitidos ({n_skip} filas)")
    return "\n".join(lines)


def run(truth: str | Path, index_path: str | Path,
        origin: tuple[float, float] | None = None,
        config: Config | None = None, limit: int | None = None,
        depot=None, enhance: bool = False,
        filas: list[dict] | None = None,
        columnas: dict[str, str] | None = None) -> AccuracyReport:
    """Geocodifica cada direccion y compara contra su coordenada verificada.

    Mismo compose que la UI: `build_geocode_query` + depot (city/CABA) +
    `fill_depot_from_index` si el form solo mando lat/lon.
    """
    from .address import (
        _address_parser, bind_parser_config, parse, unbind_parser_config,
    )
    from .depot_context import DepotContext
    from .locality import fill_depot_from_index
    from .query import build_geocode_query

    cfg = config or Config.from_env()
    bind_parser_config(cfg)
    parser = _address_parser()
    if filas is None or columnas is None:
        filas, columnas = load_truth(truth)
    reporte = AccuracyReport(source=str(truth), index=str(index_path),
                             rows_read=len(filas))

    if depot is None and origin is not None:
        depot = DepotContext(
            lat=origin[0], lon=origin[1],
            max_distance_km=float(cfg.max_geocode_distance_km),
        )
    if depot is not None:
        from .depot_context import align_depot_to_geolocator, strip_conflicting_depot_city
        from .locality import resolve_locality_near

        check_origin = origin or depot.origin
        near_city = None
        if check_origin is not None:
            near_city = resolve_locality_near(
                index_path, check_origin[0], check_origin[1],
            ).get("city")
        depot, conflict = strip_conflicting_depot_city(
            depot, check_origin,
            near_city=near_city,
            max_distance_km=float(depot.max_distance_km),
        )
        reporte.depot_warning = conflict
        depot = align_depot_to_geolocator(depot)
        depot = fill_depot_from_index(depot, index_path)
        reporte.depot_tokens = depot.enrichment_tokens() if depot else []
    effective_origin = (depot.origin if depot and depot.origin else origin)

    geocoder = None
    try:
        geocoder = LocalOSMGeocoder(
            index_path, match_threshold=cfg.match_threshold,
            low_threshold=cfg.low_confidence_threshold,
            street_level_floor=cfg.geocode_street_level_floor,
            street_match_min=cfg.geocode_street_match_min,
            review_band=cfg.geocode_review_band, valid_band=cfg.geocode_valid_band,
            aliases_path=cfg.street_aliases_path)
        for fila in (filas[:limit] if limit else filas):
            direccion = str(fila.get(columnas["address"]) or "").strip()
            lat = to_float(fila.get(columnas["lat"]))
            lon = to_float(fila.get(columnas["lng"]))
            if not direccion or not usable_truth_coord(lat, lon):
                continue          # 0,0 / fuera de WGS84: basura OA, no un fallo nuestro
            reporte.rows_usable += 1

            sent = build_geocode_query(
                direccion, row=fila, depot=depot, enhance=enhance)
            parsed = parse(sent)
            lp = getattr(parser, "last_outcome", None) or "off"
            fts = geocoder._precise_query(parsed) or geocoder._fts_query(parsed)

            resultado = geocoder.geocode(
                sent, origin=effective_origin, parsed=parsed)
            banda = band_for(resultado.status, resultado.confidence,
                             has_coords=resultado.has_coords,
                             valid_at=cfg.geocode_valid_band,
                             review_at=cfg.geocode_review_band,
                             precision=resultado.precision)
            # Producción BORRA las coordenadas de una fila que cae en
            # needs_geocoding (ver runner._stamp). Contarlas acá como "con pin"
            # inflaria la cobertura con pines que el cliente nunca ve.
            publica_pin = resultado.has_coords and banda != BAND_NEEDS_GEOCODING
            error = (haversine_km(lat, lon, resultado.lat, resultado.lon) * 1000
                     if publica_pin else None)
            conf = round(resultado.confidence, 3)

            reporte.overall.add(banda, error, confidence=conf)
            grupo = (reporte.with_number if has_house_number(direccion)
                     else reporte.without_number)
            grupo.add(banda, error, confidence=conf)

            country = str(fila.get("country") or "").strip()
            preset = str(fila.get("preset") or "").strip()
            fila_out = {
                "address": direccion,
                "sent": sent,
                "query": sent,
                "road": parsed.road or "",
                "house": parsed.house_number or "",
                "fts": fts or "",
                "truth_lat": lat,
                "truth_lng": lon,
                "pin_lat": resultado.lat if publica_pin else None,
                "pin_lng": resultado.lon if publica_pin else None,
                "error_m": round(error, 1) if error is not None else None,
                "band": banda,
                "status": resultado.status,
                "confidence": conf,
                "precision": resultado.precision or "",
                "libpostal": lp,
                "osm": (resultado.matched_text or "")[:80],
                "country": country,
                "preset": preset,
            }
            reporte.rows.append(fila_out)

            if error is not None:
                reporte.worst.append({
                    "address": direccion, "error_m": round(error, 1),
                    "band": banda, "status": resultado.status,
                    "confidence": round(resultado.confidence, 3),
                    "precision": resultado.precision,
                    "matched": (resultado.matched_text or "")[:60],
                    "truth_lat": lat, "truth_lng": lon,
                    "pin_lat": resultado.lat, "pin_lng": resultado.lon,
                    "query": sent,
                    "country": country, "preset": preset,
                })
    finally:
        stats_fn = getattr(parser, "stats", None)
        reporte.libpostal = (stats_fn() if callable(stats_fn)
                             else {"mode": "off"})
        if geocoder is not None:
            geocoder.close()
        unbind_parser_config()
    return reporte


def format_report(reporte: AccuracyReport, split: bool = True) -> str:
    """Totales: acierto (<=100 m), pin, confianza. El % es sobre el TOTAL."""
    grupos = [reporte.overall]
    if split:
        grupos = [reporte.with_number, reporte.without_number, reporte.overall]

    ancho = 20
    cabecera = (f"{'grupo':{ancho}} {'n':>6} {'acierto':>8} {'con pin':>8} "
                + " ".join(f"{'<=' + str(m) + 'm':>9}" for m in BUCKETS)
                + f" {'mediana':>9} {'conf':>6}")
    lineas = [cabecera, "-" * len(cabecera)]
    for g in grupos:
        if not g.total:
            continue
        d = g.as_dict()
        mediana = f"{d['median_m']:.0f} m" if d["median_m"] is not None else "—"
        conf = f"{d['mean_confidence']:.2f}" if d["mean_confidence"] is not None else "—"
        lineas.append(
            f"{g.name:{ancho}} {g.total:>6} {d['hit_pct']:>7.0f}% {d['pin_pct']:>7.0f}% "
            + " ".join(f"{d[f'within_{m}m_pct']:>8.0f}%" for m in BUCKETS)
            + f" {mediana:>9} {conf:>6}")

    lineas.append("")
    lineas.append(f"acierto = pin publicado a <= {HIT_M} m  (sobre el total, no sobre pines)")
    lineas.append("confianza = media de confidence del geocoder (todas las filas)")
    lineas.append("")
    lineas.append("por banda (mediana del error de lo que la banda promete):")
    for g in grupos:
        d = g.as_dict()
        for banda, dato in (d["band_quality"] or {}).items():
            lineas.append(f"  {g.name:{ancho}} {banda:16} n={dato['n']:>5}  "
                          f"mediana {dato['median_m']:>8.0f} m")
        sin = g.by_band.get(BAND_NEEDS_GEOCODING, 0)
        if sin:
            lineas.append(f"  {g.name:{ancho}} {BAND_NEEDS_GEOCODING:16} n={sin:>5}  "
                          f"{'(sin pin)':>16}")
        if d["mean_confidence_pin"] is not None:
            lineas.append(f"  {g.name:{ancho}} {'conf (con pin)':16} "
                          f"{d['mean_confidence_pin']:.2f}")
    lineas.append("")
    lineas.append(format_libpostal_report(reporte.libpostal))
    lineas.append("")
    lineas.append(format_calibration(reporte.rows))
    return "\n".join(lineas)


def format_libpostal_report(stats: dict | None) -> str:
    """Una linea de resumen: cuantas resolvio el heuristico sin libpostal."""
    s = stats or {}
    mode = s.get("mode") or "off"
    if mode == "off" and not s.get("enhancer_calls") and not s.get("enhancer_skips"):
        return "libpostal: off  (el heuristico resolvio todo, o la flag esta apagada)"
    calls = int(s.get("enhancer_calls") or 0)
    skips = int(s.get("enhancer_skips") or 0)
    helped = int(s.get("helped") or 0)
    noop = int(s.get("noop") or 0)
    total = calls + skips
    skip_pct = s.get("skip_pct")
    if skip_pct is None:
        skip_pct = round(100.0 * skips / total, 1) if total else 0.0
    lineas = [
        f"libpostal: {mode}  skip={skips} ({skip_pct:.0f}%)  "
        f"called={calls}  helped={helped}  noop={noop}",
    ]
    reasons = s.get("by_reason") or {}
    if reasons:
        lineas.append("  por que entro: " + ", ".join(
            f"{k}={v}" for k, v in sorted(reasons.items())))
    lineas.append(
        "  skip = heuristico ya tenia road  ·  helped = aporto road/altura  "
        "·  noop = entro y no cambio nada util")
    for example in (s.get("helped_examples") or [])[:3]:
        lineas.append(f"  helped: {example}")
    return "\n".join(lineas)


def calibration(rows: list[dict]) -> dict:
    """Confianza textual vs pin real. Detecta verdes lejos (falsos positivos)."""
    high = [r for r in rows if _conf(r) >= DEFAULT_VALID_AT]
    valid = [r for r in rows if r.get("band") == BAND_VALID]
    over = [r for r in valid if _error_m(r) is not None and _error_m(r) > OVERCONFIDENT_M]
    street_far = [
        r for r in rows
        if (r.get("precision") or "") in ("street", "street_mismatch", "street_weak")
        and _error_m(r) is not None and _error_m(r) > OVERCONFIDENT_M
    ]
    high_miss = [r for r in high if r.get("pin_lat") is None]
    high_hit = [r for r in high if _error_m(r) is not None and _error_m(r) <= HIT_M]
    return {
        "high_conf": len(high),
        "high_conf_hit_100m": len(high_hit),
        "high_conf_hit_pct": _pct(len(high_hit), len(high)),
        "high_conf_no_pin": len(high_miss),
        "valid": len(valid),
        "valid_overconfident": len(over),
        "street_far": len(street_far),
        "overconfident_m": OVERCONFIDENT_M,
    }


def format_calibration(rows: list[dict]) -> str:
    """Conf ≠ acierto: el score mide texto, no metros."""
    c = calibration(rows)
    lineas = [
        "calibracion (conf es parecido textual al OSM, NO metros al truth):",
        f"  conf>={DEFAULT_VALID_AT:.2f}  n={c['high_conf']:>5}  "
        f"acierto<={HIT_M}m={c['high_conf_hit_pct']:.0f}%  "
        f"sin pin={c['high_conf_no_pin']}",
        f"  banda valid     n={c['valid']:>5}  "
        f"lejos(>{OVERCONFIDENT_M}m)={c['valid_overconfident']}"
        + ("  ← falso positivo: el operador no revisa un verde" if c["valid_overconfident"]
           else "  (ningun verde lejos)"),
        f"  prec=street     lejos(>{OVERCONFIDENT_M}m)={c['street_far']}"
        "  (esperado: centroide de calle, banda=review)",
    ]
    return "\n".join(lineas)


def _conf(row: dict) -> float:
    try:
        return float(row.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _error_m(row: dict) -> float | None:
    value = row.get("error_m")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clip(text: str, width: int) -> str:
    text = (text or "").replace("\n", " ")
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _coord(lat, lng) -> tuple[str, str]:
    if lat is None or lng is None:
        return "—", "—"
    return f"{float(lat):.5f}", f"{float(lng):.5f}"


def _filter_rows(rows: list[dict], only: str) -> list[dict]:
    """only: all | house | pin | miss | far | over."""
    out = []
    for row in rows:
        tiene = bool(row.get("house") or has_house_number(row.get("address") or ""))
        tiene_pin = row.get("pin_lat") is not None
        error = _error_m(row)
        if only == "house" and not tiene:
            continue
        if only == "pin" and not tiene_pin:
            continue
        if only == "miss" and tiene_pin:
            continue
        if only == "far" and (error is None or error <= OVERCONFIDENT_M):
            continue
        if only == "over" and not (
                row.get("band") == BAND_VALID
                and error is not None and error > OVERCONFIDENT_M):
            continue
        out.append(row)
    return out


def _row_dump_section(row: dict) -> str:
    """ok | far | miss — para ordenar y separar el dump."""
    if row.get("pin_lat") is None or _error_m(row) is None:
        return "miss"
    if _error_m(row) > OVERCONFIDENT_M:
        return "far"
    return "ok"


def _sort_rows_for_dump(rows: list[dict]) -> list[dict]:
    """Cerca primero; al final las más lejos y, después, las sin pin."""
    rank = {"ok": 0, "far": 1, "miss": 2}

    def key(row: dict) -> tuple:
        section = _row_dump_section(row)
        err = _error_m(row)
        return (rank[section], err if err is not None else 0.0)

    return sorted(rows, key=key)


def table_data_row_count(tabla: str) -> int:
    """Filas de datos (sin cabecera, separador ni banners ---)."""
    lines = tabla.splitlines()
    return sum(1 for ln in lines[2:] if ln and not ln.startswith("---"))


def format_row_line(row: dict) -> str:
    """Una fila de la tabla (sin cabecera)."""
    tlat, tlng = _coord(row.get("truth_lat"), row.get("truth_lng"))
    plat, plng = _coord(row.get("pin_lat"), row.get("pin_lng"))
    err = f"{row['error_m']:.0f}" if row.get("error_m") is not None else "—"
    conf = row.get("confidence")
    conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "—"
    address = _clip(str(row.get("address") or ""), _ADDR_W)
    sent = _clip(str(row.get("sent") or row.get("query") or ""), _ADDR_W)
    prec = _PREC_SHORT.get((row.get("precision") or "").lower(),
                           (row.get("precision") or "—")[:4] or "—")
    lp = (row.get("libpostal") or "off")[:5]
    return (
        f"{tlat:>10} {tlng:>10} {plat:>10} {plng:>10} {err:>7} {conf_s:>5} "
        f"{prec:>4} {lp:>5} {address:<{_ADDR_W}} {sent}"
    )


def format_rows_table(rows: list[dict], *, only: str = "all") -> str:
    """Tabla: coord original, pin, metros, address in, address enviado al geo.

    Orden: aciertos cerca → lejos (>250 m) → sin pin. Así el final del log
    es justo lo que hay que curar.
    """
    filtradas = _sort_rows_for_dump(_filter_rows(rows, only))
    w = _ADDR_W
    cabecera = (
        f"{'truth_lat':>10} {'truth_lng':>10} {'pin_lat':>10} {'pin_lng':>10} "
        f"{'m':>7} {'conf':>5} {'prec':>4} {'lp':>5} {'address':<{w}} sent"
    )
    lineas = [cabecera, "-" * max(len(cabecera), 110)]
    banners = {
        "far": f"--- lejos (>{OVERCONFIDENT_M:.0f} m), peor al final ---",
        "miss": "--- sin pin ---",
    }
    prev = None
    for row in filtradas:
        section = _row_dump_section(row)
        if section != prev and section in banners:
            lineas.append(banners[section])
        prev = section
        lineas.append(format_row_line(row))
    return "\n".join(lineas)


def format_dump_line(row: dict) -> str:
    """Compat: una linea de la tabla (sin cabecera)."""
    return format_row_line(row)


def dump_rows(rows: list[dict], *, only: str = "all") -> list[str]:
    """Lineas de la tabla incluyendo cabecera."""
    return format_rows_table(rows, only=only).splitlines()
