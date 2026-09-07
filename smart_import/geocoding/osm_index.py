"""Construye un indice de direcciones SQLite a partir de un .osm.pbf.

El PBF se recorre UNA vez y se guarda un indice liviano. Despues, geocodificar
50.000 direcciones consulta SQLite, no vuelve a abrir el PBF ni una sola vez.

No se usan los .graphml del cutter: esos ya perdieron los tags de OSM. La fuente
tiene que ser el PBF original.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..logging_setup import get_logger, stage
from .address import normalize_text

logger = get_logger("index")

SCHEMA_SQL = """
PRAGMA journal_mode = OFF;
PRAGMA synchronous = OFF;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS places (
    id            INTEGER PRIMARY KEY,
    osm_type      TEXT NOT NULL,
    osm_id        INTEGER NOT NULL,
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    kind          TEXT,
    name          TEXT,
    house_number  TEXT,
    street        TEXT,
    city          TEXT,
    district      TEXT,
    state         TEXT,
    postcode      TEXT,
    country       TEXT,
    normalized_text TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS places_fts
    USING fts5(normalized_text, content='places', content_rowid='id', tokenize='unicode61');

CREATE VIRTUAL TABLE IF NOT EXISTS places_rtree
    USING rtree(id, min_lat, max_lat, min_lon, max_lon);

CREATE INDEX IF NOT EXISTS idx_places_street ON places(street);
CREATE INDEX IF NOT EXISTS idx_places_postcode ON places(postcode);
"""

ADDR_TAGS = {
    "addr:housenumber": "house_number", "addr:street": "street", "addr:city": "city",
    "addr:district": "district", "addr:suburb": "district", "addr:state": "state",
    "addr:postcode": "postcode", "addr:country": "country",
}
NAMED_KINDS = ("highway", "place", "building", "amenity", "shop", "office",
               "tourism", "leisure", "landuse")
BATCH = 5_000


@dataclass
class BuildStats:
    nodes: int = 0
    ways: int = 0
    with_address: int = 0
    named: int = 0
    skipped: int = 0

    def as_dict(self) -> dict:
        return {"nodes": self.nodes, "ways": self.ways,
                "with_address": self.with_address, "named": self.named,
                "total": self.nodes + self.ways}


def _record(tags, lat: float, lon: float, osm_type: str, osm_id: int) -> tuple | None:
    fields = {v: None for v in set(ADDR_TAGS.values())}
    for tag, field in ADDR_TAGS.items():
        if tag in tags:
            fields[field] = tags[tag].strip() or None

    name = (tags.get("name") or "").strip() or None
    has_address = bool(fields["street"] or fields["postcode"])
    kind = next((k for k in NAMED_KINDS if k in tags), None)

    # sin direccion y sin nombre util no aporta nada al geocoder
    if not has_address and not (name and kind):
        return None

    searchable = " ".join(p for p in (
        fields["house_number"], fields["street"], name, fields["district"],
        fields["city"], fields["state"], fields["postcode"], fields["country"],
    ) if p)
    normalized = normalize_text(searchable)
    if not normalized:
        return None

    return (osm_type, osm_id, lat, lon, kind, name,
            fields["house_number"], fields["street"], fields["city"],
            fields["district"], fields["state"], fields["postcode"], fields["country"],
            normalized)


def _way_centroid(way) -> tuple[float, float] | None:
    """Punto representativo de un way (calle o edificio): promedio de sus nodos.

    Para un edificio equivale al centro; para una calle, a su punto medio, que es
    lo correcto cuando solo se conoce el nombre de la calle.
    """
    lats, lons = [], []
    for node in way.nodes:
        try:
            loc = node.location
            if loc.valid():
                lats.append(loc.lat)
                lons.append(loc.lon)
        except Exception:
            continue
    if not lats:
        return None
    return sum(lats) / len(lats), sum(lons) / len(lons)


def build(pbf_path: str | Path, output: str | Path,
          location_index: str = "flex_mem", progress=None) -> BuildStats:
    import osmium

    src = Path(pbf_path)
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    stage(logger, "INDEX", "construyendo indice desde PBF (se hace UNA vez por region)",
          pbf=src.name, tamano=f"{src.stat().st_size / 1e6:.1f}MB")
    conn = sqlite3.connect(out)
    conn.executescript(SCHEMA_SQL)
    stats = BuildStats()
    batch: list[tuple] = []

    def flush() -> None:
        if not batch:
            return
        conn.executemany(
            "INSERT INTO places (osm_type, osm_id, lat, lon, kind, name, house_number,"
            " street, city, district, state, postcode, country, normalized_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        batch.clear()

    processor = osmium.FileProcessor(str(src)).with_locations(location_index)
    for obj in processor:
        tags = obj.tags
        if not tags:
            continue

        if obj.is_node():
            rec = _record(tags, obj.location.lat, obj.location.lon, "node", obj.id)
            if rec:
                stats.nodes += 1
        elif obj.is_way():
            point = _way_centroid(obj)
            if point is None:
                stats.skipped += 1
                continue
            rec = _record(tags, point[0], point[1], "way", obj.id)
            if rec:
                stats.ways += 1
        else:
            continue

        if not rec:
            continue
        if rec[7] or rec[11]:
            stats.with_address += 1
        if rec[5]:
            stats.named += 1
        batch.append(rec)
        if len(batch) >= BATCH:
            flush()
            if progress:
                progress(stats)

    flush()

    # FTS y RTree se pueblan al final: es mucho mas rapido que indexar fila a fila
    stage(logger, "INDEX", "poblando FTS5 y RTree", lugares=stats.nodes + stats.ways)
    conn.execute("INSERT INTO places_fts(rowid, normalized_text) "
                 "SELECT id, normalized_text FROM places")
    conn.execute("INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon) "
                 "SELECT id, lat, lat, lon, lon FROM places")
    conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", [
        ("source_pbf", str(src)), ("source_name", src.name),
        ("places", str(stats.nodes + stats.ways)),
        ("with_address", str(stats.with_address)), ("named", str(stats.named)),
    ])
    conn.commit()
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.close()
    stage(logger, "INDEX", "listo", indice=out.name,
          tamano=f"{out.stat().st_size / 1e6:.1f}MB",
          con_direccion=stats.with_address, con_nombre=stats.named)
    return stats


def index_path_for(entry, index_dir: str | Path) -> Path:
    """Nombre estable derivado del PBF: el mismo extract da siempre el mismo indice."""
    return Path(index_dir).expanduser() / f"{entry.key}.sqlite"


_INDEX_BBOX_RE = re.compile(
    r"n(?P<north>-?\d+(?:\.\d+)?)_s(?P<south>-?\d+(?:\.\d+)?)"
    r"_e(?P<east>-?\d+(?:\.\d+)?)_w(?P<west>-?\d+(?:\.\d+)?)"
)


def covering_extract_index(
    index_dir: str | Path,
    lat: float,
    lon: float,
    bbox: tuple[float, float, float, float] | None = None,
) -> Path | None:
    """Indice de extract (n…_s…_e…_w….sqlite) que cubre el punto, el más chico.

    Si desapareció el PBF del extract, el país (argentina.sqlite) no debe
    sustituirlo: CABA ya tiene índice y no hay que reconstruir 400 MB.
    """
    root = Path(index_dir).expanduser()
    if not root.is_dir():
        return None
    covering: list[tuple[float, Path]] = []
    for path in root.glob("n*.sqlite"):
        match = _INDEX_BBOX_RE.search(path.name)
        if not match or path.stat().st_size < 1_000_000:
            continue
        north = float(match.group("north"))
        south = float(match.group("south"))
        east = float(match.group("east"))
        west = float(match.group("west"))
        if not (south <= lat <= north and west <= lon <= east):
            continue
        if bbox is not None:
            bn, bs, be, bw = bbox
            if not (south <= bs and north >= bn and west <= bw and east >= be):
                continue
        covering.append((abs(north - south) * abs(east - west), path))
    if not covering:
        return None
    covering.sort(key=lambda item: item[0])
    return covering[0][1]


def prefer_index(resolved: Path, index_dir: str | Path, lat: float, lon: float,
                 bbox: tuple[float, float, float, float] | None = None) -> Path:
    """Extract local gana sobre índice de país (argentina.sqlite / florida.sqlite)."""
    extract = covering_extract_index(index_dir, lat, lon, bbox)
    if extract is None:
        return resolved
    name = resolved.name.lower()
    looks_country = _INDEX_BBOX_RE.search(resolved.name) is None
    if looks_country or not resolved.exists():
        return extract
    return resolved
