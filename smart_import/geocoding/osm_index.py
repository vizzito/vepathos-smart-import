"""Construye un indice de direcciones SQLite a partir de un .osm.pbf.

El PBF se recorre UNA vez y se guarda un indice liviano. Despues, geocodificar
50.000 direcciones consulta SQLite, no vuelve a abrir el PBF ni una sola vez.

No se usan los .graphml del cutter: esos ya perdieron los tags de OSM. La fuente
tiene que ser el PBF original.
"""
from __future__ import annotations

import fcntl
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..logging_setup import get_logger, stage
from .address import normalize_text

logger = get_logger("index")

#: Filtro barato para no abrir sqlite por cada archivo del glob: un sqlite con
#: esquema no baja de unos KB. Quien decide de verdad si el indice sirve es
#: `index_is_complete`, que lo consulta como lo va a consultar el geocoder.
MIN_INDEX_BYTES = 4_096

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


def index_is_complete(path: str | Path) -> bool:
    """True si el indice existe y esta ENTERO.

    `exists()` no alcanza: un OOM-kill o un `docker stop` a mitad del build
    dejaba un sqlite truncado en la ruta final y todos los geocodes siguientes
    lo usaban en silencio. Se verifica la marca que escribe el build y, para los
    indices construidos antes de que esa marca existiera, que las dos tablas que
    consulta el geocoder tengan datos.
    """
    p = Path(path).expanduser()
    try:
        if not p.is_file() or p.stat().st_size < MIN_INDEX_BYTES:
            return False
    except OSError:
        return False
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        marca = conn.execute(
            "SELECT value FROM meta WHERE key = 'build_completed'").fetchone()
        if marca and str(marca[0]) == "1":
            return True
        # Indice anterior a la marca: no se reconstruye si sirve.
        fila = conn.execute(
            "SELECT normalized_text FROM places"
            " WHERE normalized_text <> '' LIMIT 1").fetchone()
        if fila is None:
            return False                  # build cortado antes de poblar places
        # `places_fts` es external-content: leerla directo devuelve filas de
        # `places` aunque el indice FTS este vacio. La unica prueba de que el
        # FTS se poblo es buscar como busca el geocoder.
        termino = str(fila[0]).split()[0].replace('"', "")
        if not termino:
            return False
        hit = conn.execute(
            "SELECT rowid FROM places_fts WHERE places_fts MATCH ? LIMIT 1",
            (f'"{termino}"',)).fetchone()
        return hit is not None
    except sqlite3.DatabaseError:
        return False                      # truncado, corrupto o sin esquema
    finally:
        conn.close()


def build(pbf_path: str | Path, output: str | Path,
          location_index: str = "flex_mem", progress=None) -> BuildStats:
    """Construye el indice de forma ATOMICA y bajo lock.

    Se escribe en un temporal y se renombra al final: hasta que el build no
    termina, la ruta definitiva no existe. El lock evita que dos jobs de la
    misma ciudad se pisen — el segundo borraba el archivo que el primero estaba
    escribiendo. Es el mismo patron que ya usaba el corte del PBF.
    """
    src = Path(pbf_path)
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)

    lock_dir = out.parent / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / f"{out.name}.lock", "a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        if index_is_complete(out):
            # Otro proceso lo construyo mientras esperabamos el lock.
            stage(logger, "INDEX", "indice reutilizado (lo construyo otro job)",
                  indice=out.name)
            return _stats_from_meta(out)
        return _build_locked(src, out, location_index, progress)


def _stats_from_meta(index: Path) -> BuildStats:
    """BuildStats de un indice ya construido, para no mentir con ceros."""
    stats = BuildStats()
    try:
        conn = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    except sqlite3.Error:
        return stats
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        stats.nodes = int(meta.get("places", 0) or 0)
        stats.with_address = int(meta.get("with_address", 0) or 0)
        stats.named = int(meta.get("named", 0) or 0)
    except (sqlite3.DatabaseError, ValueError):
        pass
    finally:
        conn.close()
    return stats


def _build_locked(src: Path, out: Path, location_index: str, progress) -> BuildStats:
    import osmium

    # El temporal lleva el pid: dos procesos nunca comparten archivo, y un
    # temporal huerfano de una corrida muerta no se confunde con el indice.
    tmp = out.with_name(f"{out.name}.tmp.{os.getpid()}")
    tmp.unlink(missing_ok=True)

    stage(logger, "INDEX", "construyendo indice desde PBF (se hace UNA vez por region)",
          pbf=src.name, tamano=f"{src.stat().st_size / 1e6:.1f}MB")
    conn = sqlite3.connect(tmp)
    try:
        stats = _fill(conn, src, location_index, progress)
    except BaseException:
        # Un build a medias no puede quedar en ningun lado: ni en la ruta final
        # (la usaria el proximo geocode) ni como temporal huerfano.
        conn.close()
        tmp.unlink(missing_ok=True)
        raise

    conn.close()
    os.replace(tmp, out)                  # atomico: recien aca aparece el indice
    stage(logger, "INDEX", "listo", indice=out.name,
          tamano=f"{out.stat().st_size / 1e6:.1f}MB",
          con_direccion=stats.with_address, con_nombre=stats.named)
    return stats


def _fill(conn: sqlite3.Connection, src: Path, location_index: str,
          progress) -> BuildStats:
    import osmium

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
        # Ultima escritura del build: si falta, el indice quedo a medias.
        ("build_completed", "1"),
    ])
    conn.commit()
    conn.execute("PRAGMA journal_mode = DELETE")
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
        # Un indice truncado por un build interrumpido no puede sustituir al
        # del pais: daria menos resultados sin que nadie se entere.
        if not match or not index_is_complete(path):
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
