"""Regresion fila por fila del geocoder, por el MISMO camino que produccion.

Por que existe (2026-09-15): `geocode-accuracy` llama a `LocalOSMGeocoder` directo
y se saltea cuatro cosas que el producto SI hace en `geocoding.runner.run`: el gate
de evidencia de direccion, el `force_review` de las bandas, los geofences y el
reintento limpio. Una regresion del producto ('alvarado 471' descartada sin
buscar) no aparecia en ninguna medicion porque el instrumento media otra cosa.

Este modulo no reimplementa nada del camino de produccion:

  1. escribe un CSV como el normalizado (`delivery_id`, `address` [+ localidad]),
  2. arma el depot como el worker (`depot_from_params` → `align_depot_to_geolocator`
     → indice → `fill_depot_from_index`),
  3. corre `geocoding.runner.run` con una cache temporal (nada de resultados viejos),
  4. califica cada fila contra su verdad: coordenada, calle esperada, esquina
     esperada o "no deberia tener pin",
  5. guarda un snapshot JSONL por suite y compara dos snapshots.

La comparacion es por NOTA de fila, no por promedio: un cambio que mejora 40 filas
y rompe 3 aparece como 3 regresiones con nombre y apellido, no como "+37".
"""
from __future__ import annotations

import csv
import dataclasses
import json
import math
import sqlite3
import statistics
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "examples" / "geocode-truth" / "regression" / "manifest.json"

#: pin a no mas de esto de la verdad = acierto (mismo corte que geocode-accuracy)
GOOD_M = 100.0
#: hasta aca es "cerca" (cuadra equivocada, centroide de calle); mas lejos, pin malo
FAIR_M = 500.0

#: Nota de cada resultado. Ordena lo que le pasa al operador, no al geocoder:
#: un verde lejos es el peor error (nadie lo revisa) y un ambar lejos es malo pero
#: visible; "sin pin" es neutro (se ubica a mano).
GRADES = {
    "green_good": 6,
    "none_expected": 6,
    "amber_good": 5,
    "amber_fair": 4,
    "none": 3,
    "green_fair": 2,
    "amber_bad": 1,
    "green_bad": 0,
}

PIN_BANDS = ("valid", "review")


# --------------------------------------------------------------------------- #
# Carga de corpus
# --------------------------------------------------------------------------- #

@dataclass
class Expectation:
    """Que deberia pasar con una fila.

    kind:
      point        → pin cerca de (lat, lng)
      street       → pin sobre alguna de `streets` (nodos del indice)
      intersection → pin cerca del cruce de `streets` (lat/lng = punto del cruce)
      none         → la fila NO es una direccion: ningun pin es correcto
      skip         → se registra pero no se califica (verdad incierta)
    """
    kind: str
    lat: float | None = None
    lng: float | None = None
    streets: tuple[str, ...] = ()
    good_m: float | None = None
    fair_m: float | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.lat is not None:
            out["lat"], out["lng"] = self.lat, self.lng
        if self.streets:
            out["streets"] = list(self.streets)
        return out


@dataclass
class CaseRow:
    idx: int
    address: str
    expect: Expectation
    locality: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _usable_coord(lat: float | None, lng: float | None) -> bool:
    return (lat is not None and lng is not None and -90 <= lat <= 90
            and -180 <= lng <= 180 and not (abs(lat) < 1e-6 and abs(lng) < 1e-6))


_META_KEYS = ("country", "preset", "noise_level", "noise_kind", "noise_variant",
              "style", "note", "id", "source_id", "address_clean")
_LOCALITY_KEYS = ("zone", "city", "region", "postcode", "country")
#: como las mapearia DETECT en un export real (neighborhood → zone, …)
_LOCALITY_ALIASES = {
    "neighborhood": "zone", "neighbourhood": "zone", "barrio": "zone",
    "locality": "zone", "ciudad": "city", "localidad": "city",
    "province": "region", "provincia": "region", "state": "region",
    "zip": "postcode", "zipcode": "postcode", "cp": "postcode",
}


def _expect_from_row(row: dict[str, Any]) -> Expectation | None:
    raw = row.get("expect")
    if isinstance(raw, dict):
        kind = str(raw.get("kind") or "").strip().lower()
        streets = raw.get("streets") or ([raw["street"]] if raw.get("street") else [])
        return Expectation(
            kind=kind, lat=_num(raw.get("lat")), lng=_num(raw.get("lng")),
            streets=tuple(str(s) for s in streets),
            good_m=_num(raw.get("good_m")), fair_m=_num(raw.get("fair_m")))
    truth = row.get("truth") if isinstance(row.get("truth"), dict) else None
    lat = _num((truth or row).get("lat", row.get("truth_lat")))
    lng = _num((truth or row).get("lng", row.get("lon", row.get("truth_lng"))))
    if _usable_coord(lat, lng):
        return Expectation(kind="point", lat=lat, lng=lng,
                           good_m=_num(row.get("max_error_m")))
    return None


def _json_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("cases", "rows", "stops", "addresses"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
    raise ValueError("JSON sin lista de filas (cases/rows/stops/addresses)")


def load_cases(path: str | Path, *, keep_locality: bool = False) -> list[CaseRow]:
    """Filas calificables de un corpus JSON/CSV/XLSX."""
    p = Path(path)
    if p.suffix.lower() == ".json":
        rows = _json_rows(json.loads(p.read_text(encoding="utf-8")))
        address_key = "address"
    else:
        from ..geocoding.accuracy import load_truth

        rows, columns = load_truth(p)
        address_key = columns["address"]
        rows = [{**r, "address": r.get(address_key),
                 "lat": r.get(columns["lat"]), "lng": r.get(columns["lng"])}
                for r in rows]
        address_key = "address"

    out: list[CaseRow] = []
    for i, row in enumerate(rows):
        address = str(row.get(address_key) or "").strip()
        if not address:
            continue
        expect = _expect_from_row(row)
        if expect is None:
            continue
        locality: dict[str, str] = {}
        if keep_locality:
            for key, value in row.items():
                target = key if key in _LOCALITY_KEYS else _LOCALITY_ALIASES.get(str(key).lower())
                text = str(value).strip() if value not in (None, "") else ""
                if target and text and target not in locality:
                    locality[target] = text
        meta = {k: row[k] for k in _META_KEYS if row.get(k) not in (None, "")}
        out.append(CaseRow(idx=i, address=address, expect=expect,
                           locality=locality, meta=meta))
    return out


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #

@dataclass
class Suite:
    id: str
    corpus: Path
    depot: dict[str, Any]
    profiles: tuple[str, ...] = ("fast", "full")
    limits: dict[str, int] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    keep_locality: bool = False
    good_m: float = GOOD_M
    fair_m: float = FAIR_M
    index: str | None = None
    #: margen del extract con el que se construyo el indice de la suite (corpus OSM)
    index_margin_km: float | None = None
    note: str = ""

    def limit_for(self, profile: str) -> int | None:
        value = self.limits.get(profile)
        return int(value) if value else None


def load_manifest(path: str | Path = DEFAULT_MANIFEST) -> list[Suite]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    base = p.parent
    suites: list[Suite] = []
    for raw in data.get("suites") or ():
        corpus = Path(raw["corpus"])
        if not corpus.is_absolute():
            corpus = (base / corpus).resolve() if (base / corpus).exists() \
                else (ROOT / corpus).resolve()
        suites.append(Suite(
            id=raw["id"], corpus=corpus, depot=dict(raw.get("depot") or {}),
            profiles=tuple(raw.get("profiles") or ("fast", "full")),
            limits=dict(raw.get("limits") or {}),
            tags=tuple(raw.get("tags") or ()),
            keep_locality=bool(raw.get("keep_locality", False)),
            good_m=float(raw.get("good_m", GOOD_M)),
            fair_m=float(raw.get("fair_m", FAIR_M)),
            index=raw.get("index"),
            index_margin_km=_num(raw.get("index_margin_km")),
            note=str(raw.get("note") or ""),
        ))
    return suites


# --------------------------------------------------------------------------- #
# Calificacion
# --------------------------------------------------------------------------- #

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


class StreetProbe:
    """Distancia de un pin a los nodos de una calle del indice (addr + ways)."""

    def __init__(self, index_path: str | Path):
        self._conn = sqlite3.connect(f"file:{Path(index_path)}?mode=ro", uri=True)
        self._cache: dict[str, list[tuple[float, float]]] = {}

    def close(self) -> None:
        self._conn.close()

    def points(self, street: str) -> list[tuple[float, float]]:
        if street not in self._cache:
            rows = self._conn.execute(
                "SELECT lat, lon FROM places WHERE street = ?"
                " OR (kind = 'highway' AND name = ?)", (street, street)).fetchall()
            pts = [(float(a), float(b)) for a, b in rows]
            try:
                from ..geocoding.interpolate import parse_polyline
                for (raw,) in self._conn.execute(
                        "SELECT geom FROM places WHERE kind = 'highway' AND name = ?"
                        " AND geom IS NOT NULL", (street,)):
                    pts.extend(parse_polyline(raw))
            except sqlite3.OperationalError:
                pass                      # indice viejo sin columna geom
            self._cache[street] = pts
        return self._cache[street]

    def distance_m(self, streets: Iterable[str], lat: float, lng: float) -> float | None:
        best: float | None = None
        for street in streets:
            for plat, plng in self.points(street):
                d = haversine_m(lat, lng, plat, plng)
                if best is None or d < best:
                    best = d
        return best


def grade_row(expect: Expectation, band: str, pin: tuple[float, float] | None,
              *, good_m: float, fair_m: float,
              probe: StreetProbe | None = None) -> tuple[str, float | None]:
    """(outcome, distancia en metros) de una fila ya geocodificada."""
    has_pin = pin is not None and band in PIN_BANDS
    color = "green" if band == "valid" else "amber"
    if expect.kind == "skip":
        return ("skip", None)
    if expect.kind == "none":
        if not has_pin:
            return ("none_expected", None)
        return (f"{color}_bad", None)
    if not has_pin:
        return ("none", None)

    lat, lng = pin
    good = expect.good_m or good_m
    fair = max(expect.fair_m or fair_m, good)
    dist: float | None
    if expect.kind in ("point", "intersection"):
        dist = haversine_m(lat, lng, expect.lat, expect.lng)
    elif expect.kind == "street":
        dist = probe.distance_m(expect.streets, lat, lng) if probe else None
        if dist is None:
            return ("skip", None)
    else:
        raise ValueError(f"expectativa desconocida: {expect.kind}")
    if dist <= good:
        return (f"{color}_good", dist)
    if dist <= fair:
        return (f"{color}_fair", dist)
    return (f"{color}_bad", dist)


# --------------------------------------------------------------------------- #
# Ejecucion por el camino del producto
# --------------------------------------------------------------------------- #

@dataclass
class SuiteRun:
    suite: str
    index: str
    depot_tokens: list[str]
    rows: list[dict[str, Any]]
    elapsed_s: float
    report: dict[str, Any]


def product_config(base=None):
    """Config de produccion: umbrales por env + libpostal on-demand como el container."""
    from ..config import Config

    cfg = base or Config.from_env()
    try:
        from ..addresses.libpostal_parser import is_installed
        libpostal = is_installed()
    except Exception:                     # pragma: no cover - entorno sin postal
        libpostal = False
    return dataclasses.replace(cfg, libpostal_enabled=libpostal)


def run_suite(suite: Suite, *, profile: str = "fast", cfg=None,
              workdir: str | Path | None = None,
              limit: int | None = None) -> SuiteRun:
    from ..geocoding import runner
    from ..geocoding.address import bind_parser_config, unbind_parser_config
    from ..geocoding.depot_context import align_depot_to_geolocator, depot_from_params
    from ..geocoding.extract import ensure_geocode_index_from_config
    from ..geocoding.locality import fill_depot_from_index

    cfg = product_config(cfg)
    if suite.index_margin_km:
        cfg = dataclasses.replace(cfg, extract_margin_km=suite.index_margin_km)
    cases = load_cases(suite.corpus, keep_locality=suite.keep_locality)
    cap = limit if limit is not None else suite.limit_for(profile)
    if cap:
        cases = cases[:cap]
    if not cases:
        raise ValueError(f"{suite.id}: el corpus no tiene filas calificables")

    d = suite.depot
    depot = depot_from_params(
        origin_lat=_num(d.get("lat")), origin_lon=_num(d.get("lon")),
        depot_city=d.get("city"), depot_region=d.get("region"),
        depot_postcode=d.get("postcode"), depot_country=d.get("country"),
        depot_address=d.get("address"),
        max_distance_km=float(d.get("max_distance_km") or cfg.max_geocode_distance_km))
    depot = align_depot_to_geolocator(depot)
    origin = depot.origin if depot is not None else None

    if suite.index:
        index_path = Path(suite.index)
        if not index_path.is_absolute():
            index_path = Path(cfg.index_dir) / index_path
        country_slug = None
    else:
        zone_hint = " ".join(str(p).strip() for p in (
            depot.country, depot.region, depot.city) if p and str(p).strip()) or None
        ready = ensure_geocode_index_from_config(
            cfg, lat=origin[0] if origin else None, lon=origin[1] if origin else None,
            bbox=None, zone_hint=zone_hint)
        index_path, country_slug = ready.path, ready.country_slug
    depot = fill_depot_from_index(depot, index_path, country_slug=country_slug)

    tmp_root = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="georeg_"))
    tmp_root.mkdir(parents=True, exist_ok=True)
    src = tmp_root / f"{suite.id}.normalized.csv"
    dst = tmp_root / f"{suite.id}.geocoded.csv"
    cache = tmp_root / f"{suite.id}.cache.sqlite"
    cache.unlink(missing_ok=True)

    locality_cols = sorted({k for c in cases for k in c.locality})
    with src.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["delivery_id", "address", *locality_cols])
        for case in cases:
            writer.writerow([f"REG-{case.idx:06d}", case.address,
                             *[case.locality.get(k, "") for k in locality_cols]])

    bind_parser_config(cfg)
    started = time.perf_counter()
    try:
        report = runner.run(src, dst, index_path, origin=origin, bbox=None,
                            config=cfg, cache_path=cache, depot=depot,
                            enhance_addresses=False)
    finally:
        unbind_parser_config()
    elapsed = time.perf_counter() - started

    with dst.open(encoding="utf-8", newline="") as fh:
        produced = {r["delivery_id"]: r for r in csv.DictReader(fh)}

    probe = StreetProbe(index_path)
    rows: list[dict[str, Any]] = []
    try:
        for case in cases:
            got = produced.get(f"REG-{case.idx:06d}") or {}
            band = (got.get("geocode_band") or "needs_geocoding").strip()
            lat, lng = _num(got.get("lat")), _num(got.get("lng"))
            pin = (lat, lng) if _usable_coord(lat, lng) else None
            outcome, dist = grade_row(case.expect, band, pin, good_m=suite.good_m,
                                      fair_m=suite.fair_m, probe=probe)
            rows.append({
                "key": f"{suite.id}:{case.idx}",
                "suite": suite.id,
                "idx": case.idx,
                "address": case.address,
                "expect": case.expect.as_dict(),
                "band": band,
                "status": got.get("geocode_status") or "",
                "confidence": _num(got.get("geocode_confidence") or got.get("geocode_raw_score")),
                "precision": got.get("geocode_precision") or "",
                "reason": got.get("geocode_reason") or "",
                "pin": [round(lat, 7), round(lng, 7)] if pin else None,
                "dist_m": round(dist, 1) if dist is not None else None,
                "outcome": outcome,
                "grade": GRADES.get(outcome),
                **{k: v for k, v in case.meta.items()},
            })
    finally:
        probe.close()

    return SuiteRun(suite=suite.id, index=str(index_path),
                    depot_tokens=depot.enrichment_tokens() if depot else [],
                    rows=rows, elapsed_s=round(elapsed, 2), report=report.as_dict())


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #

def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    graded = [r for r in rows if r.get("grade") is not None]
    outcomes = Counter(r["outcome"] for r in rows)
    pinned = [r for r in rows if r.get("pin")]
    dists = [r["dist_m"] for r in graded if r.get("dist_m") is not None]
    n = len(graded) or 1
    return {
        "rows": len(rows),
        "graded": len(graded),
        "pin_pct": round(100 * len(pinned) / (len(rows) or 1), 1),
        "good_pct": round(100 * sum(1 for r in graded if r["outcome"] in (
            "green_good", "amber_good", "none_expected")) / n, 1),
        "green_good": outcomes.get("green_good", 0),
        "amber_good": outcomes.get("amber_good", 0),
        "amber_fair": outcomes.get("amber_fair", 0),
        "none": outcomes.get("none", 0),
        "none_expected": outcomes.get("none_expected", 0),
        "green_fair": outcomes.get("green_fair", 0),
        "amber_bad": outcomes.get("amber_bad", 0),
        "false_green": outcomes.get("green_bad", 0),
        "skip": outcomes.get("skip", 0),
        "grade_mean": round(sum(r["grade"] for r in graded) / n, 3),
        "median_m": round(statistics.median(dists), 1) if dists else None,
    }


def write_suite_rows(run: SuiteRun, out_dir: str | Path) -> Path:
    """Escribe una suite apenas termina: una corrida cortada deja lo hecho diffeable."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{run.suite}.jsonl"
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in run.rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return path


def write_snapshot(runs: list[SuiteRun], out_dir: str | Path, *,
                   label: str, profile: str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"label": label, "profile": profile,
                               "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                               "suites": {}}
    for run in runs:
        with (out / f"{run.suite}.jsonl").open("w", encoding="utf-8") as fh:
            for row in run.rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        summary["suites"][run.suite] = {
            **summarize(run.rows), "index": Path(run.index).name,
            "depot_tokens": run.depot_tokens, "elapsed_s": run.elapsed_s,
        }
    all_rows = [r for run in runs for r in run.rows]
    summary["total"] = summarize(all_rows)
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def read_snapshot(path: str | Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for file in sorted(Path(path).glob("*.jsonl")):
        with file.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    row = json.loads(line)
                    rows[row["key"]] = row
    return rows


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #

@dataclass
class RowChange:
    key: str
    suite: str
    address: str
    before: dict[str, Any]
    after: dict[str, Any]
    kind: str                      # regression | improvement | drift | demoted

    def line(self) -> str:
        def fmt(r: dict[str, Any]) -> str:
            dist = f"{r['dist_m']:.0f}m" if r.get("dist_m") is not None else "-"
            return (f"{r['outcome']}({r['band'][:6]},{dist},"
                    f"{(r.get('reason') or r.get('precision') or '')[:26]})")
        extra = ""
        if self.after.get("noise_kind") or self.after.get("noise_level") is not None:
            extra = f" [{self.after.get('noise_kind') or 'L' + str(self.after.get('noise_level'))}]"
        return f"{self.suite:24} {self.address[:44]:44} {fmt(self.before)} → {fmt(self.after)}{extra}"


def diff_snapshots(before: dict[str, dict], after: dict[str, dict], *,
                   drift_m: float = 30.0) -> dict[str, Any]:
    changes: list[RowChange] = []
    missing = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    for key in sorted(set(before) & set(after)):
        b, a = before[key], after[key]
        if b.get("address") != a.get("address"):
            raise ValueError(f"{key}: el corpus cambio entre snapshots "
                             f"({b.get('address')!r} vs {a.get('address')!r})")
        gb, ga = b.get("grade"), a.get("grade")
        if gb is None or ga is None:
            continue
        same_pin = (b.get("pin") and a.get("pin")
                    and haversine_m(*b["pin"], *a["pin"]) <= 1.0)
        if (ga < gb and same_pin and b.get("band") == "valid"
                and a.get("band") == "review"):
            # mismo pin, de verde a ambar: politica de bandas, no un pin peor
            changes.append(RowChange(key, a["suite"], a["address"], b, a, "demoted"))
            continue
        if ga < gb:
            changes.append(RowChange(key, a["suite"], a["address"], b, a, "regression"))
        elif ga > gb:
            changes.append(RowChange(key, a["suite"], a["address"], b, a, "improvement"))
        elif (b.get("dist_m") is not None and a.get("dist_m") is not None
              and a["dist_m"] - b["dist_m"] > drift_m):
            changes.append(RowChange(key, a["suite"], a["address"], b, a, "drift"))

    per_suite: dict[str, Counter] = defaultdict(Counter)
    for ch in changes:
        per_suite[ch.suite][ch.kind] += 1
    by_kind: dict[str, Counter] = defaultdict(Counter)
    for ch in changes:
        tag = str(ch.after.get("noise_kind") or ch.after.get("noise_level") or "-")
        by_kind[tag][ch.kind] += 1
    return {
        "changes": changes,
        "per_suite": {k: dict(v) for k, v in per_suite.items()},
        "per_noise": {k: dict(v) for k, v in by_kind.items()},
        "missing": missing,
        "added": added,
    }


def format_diff(result: dict[str, Any], *, max_improvements: int = 40,
                max_drift: int = 20) -> str:
    changes: list[RowChange] = result["changes"]
    regs = [c for c in changes if c.kind == "regression"]
    imps = [c for c in changes if c.kind == "improvement"]
    drift = [c for c in changes if c.kind == "drift"]
    demoted = [c for c in changes if c.kind == "demoted"]
    lines = [f"regresiones={len(regs)}  mejoras={len(imps)}  deriva={len(drift)}"
             f"  verde→ambar(mismo pin)={len(demoted)}"
             f"  faltan={len(result['missing'])}  nuevas={len(result['added'])}", ""]
    if result["per_suite"]:
        lines.append("por suite:")
        for suite, counts in sorted(result["per_suite"].items()):
            lines.append(f"  {suite:28} " + "  ".join(
                f"{k}={v}" for k, v in sorted(counts.items())))
        lines.append("")
    if regs:
        lines.append("REGRESIONES (todas):")
        lines.extend("  " + c.line() for c in regs)
        lines.append("")
    if imps:
        lines.append(f"mejoras (primeras {max_improvements}):")
        lines.extend("  " + c.line() for c in imps[:max_improvements])
        lines.append("")
    if drift:
        lines.append(f"deriva (> {30:.0f} m peor, misma nota; primeras {max_drift}):")
        lines.extend("  " + c.line() for c in drift[:max_drift])
        lines.append("")
    if demoted:
        by_outcome = Counter(c.before["outcome"] for c in demoted)
        lines.append("verde→ambar con el mismo pin (politica de bandas): "
                     + "  ".join(f"{k}={v}" for k, v in by_outcome.most_common()))
    return "\n".join(lines)


def format_summary(summary: dict[str, Any]) -> str:
    head = (f"{'suite':28} {'rows':>5} {'pin%':>6} {'ok%':>6} {'g_ok':>5} {'a_ok':>5}"
            f" {'a_fair':>6} {'none':>5} {'g_fair':>6} {'a_bad':>5} {'FALSEG':>6}"
            f" {'nota':>6} {'med_m':>7}")
    lines = [head, "-" * len(head)]

    def line(name: str, s: dict[str, Any]) -> str:
        return (f"{name[:28]:28} {s['rows']:5} {s['pin_pct']:6.1f} {s['good_pct']:6.1f}"
                f" {s['green_good']:5} {s['amber_good']:5} {s['amber_fair']:6}"
                f" {s['none'] + s['none_expected']:5} {s['green_fair']:6} {s['amber_bad']:5}"
                f" {s['false_green']:6} {s['grade_mean']:6.2f}"
                f" {s['median_m'] if s['median_m'] is not None else '-':>7}")

    for name, s in summary["suites"].items():
        lines.append(line(name, s))
    lines.append("-" * len(head))
    lines.append(line("TOTAL", summary["total"]))
    return "\n".join(lines)
