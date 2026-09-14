"""Alias de calles OSM: el mismo way tiene `name` + `name:fr` + `name:nl`.

El indice historico solo metia `name` y `addr:street`. En Bruselas eso deja
`Mozartstraat` y la query francesa `Avenue Mozart` no se encuentran. El mapa
se arma UNA vez recorriendo los PBF locales (tags, sin geometria) y se usa
como expansion de la query: no hace falta reconstruir cada sqlite de ciudad.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from .address import normalize_text

#: name:etymology / name:left no son el mismo objeto en otro idioma.
_SKIP_PREFIXES = (
    "name:etymology", "name:pronunciation", "name:prefix", "name:suffix",
    "name:left", "name:right", "name:botanical", "old_name", "source:name",
)
_BASE_KEYS = frozenset({
    "name", "alt_name", "official_name", "loc_name", "short_name", "addr:street",
})
_LANG_NAME = re.compile(r"^(?:name|addr:street):[a-z]{2,3}(?:-[a-z0-9]+)?$")
_SPLIT_ALT = re.compile(r"\s*;\s*")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS aliases (
    key TEXT NOT NULL,
    variant TEXT NOT NULL,
    PRIMARY KEY (key, variant)
);
CREATE INDEX IF NOT EXISTS idx_aliases_key ON aliases(key);

CREATE TABLE IF NOT EXISTS harvested (
    source TEXT PRIMARY KEY,
    size INTEGER,
    mtime REAL,
    pairs INTEGER,
    harvested_at REAL
);
"""

_EXTRACT_DIR_NAMES = frozenset({"_extracts", "extracts"})


def _iter_tags(tags) -> list[tuple[str, str]]:
    """dict de tests o TagList de osmium."""
    if not tags:
        return []
    if isinstance(tags, dict):
        return [(str(k), str(v)) for k, v in tags.items() if v]
    out: list[tuple[str, str]] = []
    try:
        for item in tags:
            if isinstance(item, tuple) and len(item) == 2:
                k, v = item
            else:
                k, v = item.k, item.v
            if v:
                out.append((str(k), str(v)))
    except TypeError:
        return []
    return out


def _is_name_key(key: str) -> bool:
    if key in _BASE_KEYS:
        return True
    if any(key == p or key.startswith(p + ":") or key.startswith(p + "_")
           for p in _SKIP_PREFIXES):
        return False
    return bool(_LANG_NAME.match(key))


def names_from_tags(tags) -> list[str]:
    """Nombres de la misma via/addr, unicos, en el orden en que aparecen."""
    seen: set[str] = set()
    out: list[str] = []
    for key, raw in _iter_tags(tags):
        if not _is_name_key(key):
            continue
        for part in _SPLIT_ALT.split(raw.strip()):
            val = part.strip()
            if not val:
                continue
            fold = val.casefold()
            if fold in seen:
                continue
            seen.add(fold)
            out.append(val)
    return out


_ALIAS_SKIP = frozenset({"alt_name", "loc_name", "short_name"})


def alias_names_from_tags(tags) -> list[str]:
    """Nombres que son LA MISMA via, no el local que vive en esa via.

    Un building `name=Administration Communale` + `addr:street=Rue de l'Église`
    no es un par bilingue: es un POI. En un highway, `name` + `name:fr` si.
    `alt_name` se deja afuera: OSM lo usa para apodos y a veces para OTRA via.
    """
    items = _iter_tags(tags)
    keys = {k for k, _ in items}
    if "highway" in keys:
        subset = {k: v for k, v in items
                  if _is_name_key(k) and k not in _ALIAS_SKIP
                  and not k.startswith("addr:")}
        return names_from_tags(subset)
    subset = {k: v for k, v in items
              if k == "addr:street" or k.startswith("addr:street:")}
    return names_from_tags(subset)


def pair_names(names: list[str]) -> list[tuple[str, str]]:
    """(clave normalizada, grafia OSM) para cada par co-ocurrente."""
    cleaned = [n.strip() for n in names if n and n.strip()]
    if len(cleaned) < 2:
        return []
    rows: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for a, b in combinations(cleaned, 2):
        for src, dst in ((a, b), (b, a)):
            key = normalize_text(src)
            if not key or key == normalize_text(dst):
                continue
            item = (key, dst)
            if item in seen:
                continue
            seen.add(item)
            rows.append(item)
    return rows


@dataclass
class HarvestStats:
    files: int = 0
    skipped: int = 0
    objects: int = 0
    pairs: int = 0
    errors: list[str] = field(default_factory=list)


class StreetAliasStore:
    """SQLite de pares name↔name:xx. Lectura en el geocoder, escritura en el barrido."""

    def __init__(self, path: str | Path, *, readonly: bool = False):
        self.path = Path(path).expanduser()
        if readonly:
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            self._conn.execute("PRAGMA query_only = ON")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path)
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        self._conn.close()

    def add_pairs(self, pairs: list[tuple[str, str]]) -> int:
        if not pairs:
            return 0
        self._conn.executemany(
            "INSERT OR IGNORE INTO aliases(key, variant) VALUES (?, ?)", pairs)
        return len(pairs)

    def add_names(self, names: list[str]) -> int:
        return self.add_pairs(pair_names(names))

    def expand(self, road: str, *, limit: int = 12) -> list[str]:
        key = normalize_text(road)
        if not key:
            return []
        rows = self._conn.execute(
            "SELECT variant FROM aliases WHERE key = ? LIMIT ?",
            (key, limit),
        ).fetchall()
        return [r[0] for r in rows if r[0]]

    def expand_many(self, roads: list[str], *, limit: int = 12) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for road in roads:
            for variant in self.expand(road, limit=limit):
                fold = variant.casefold()
                if fold in seen:
                    continue
                seen.add(fold)
                out.append(variant)
                if len(out) >= limit:
                    return out
        return out

    def already_harvested(self, source: Path) -> bool:
        try:
            st = source.stat()
        except OSError:
            return False
        row = self._conn.execute(
            "SELECT size, mtime FROM harvested WHERE source = ?",
            (str(source.resolve()),),
        ).fetchone()
        if not row:
            return False
        return int(row[0]) == int(st.st_size) and abs(float(row[1]) - st.st_mtime) < 1.0

    def mark_harvested(self, source: Path, pairs: int) -> None:
        st = source.stat()
        self._conn.execute(
            "INSERT OR REPLACE INTO harvested(source, size, mtime, pairs, harvested_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (str(source.resolve()), int(st.st_size), st.st_mtime, int(pairs), time.time()),
        )
        self._conn.commit()

    def pair_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM aliases").fetchone()
        return int(row[0]) if row else 0

    def commit(self) -> None:
        self._conn.commit()


def open_alias_store(path: str | Path | None) -> StreetAliasStore | None:
    """None si no hay archivo: el geocoder sigue igual que antes."""
    if not path:
        return None
    target = Path(path).expanduser()
    if not target.is_file() or target.stat().st_size < 64:
        return None
    try:
        return StreetAliasStore(target, readonly=True)
    except (sqlite3.Error, FileNotFoundError, OSError):
        return None


def _should_skip_pbf(path: Path, *, include_extracts: bool) -> bool:
    name = path.name
    if ".tmp." in name or name.startswith("."):
        return True
    if include_extracts:
        return False
    return any(part in _EXTRACT_DIR_NAMES for part in path.parts)


def iter_pbfs(root: str | Path, *, include_extracts: bool = False,
              only: tuple[str, ...] = ()) -> list[Path]:
    base = Path(root).expanduser()
    if not base.is_dir():
        return []
    found = [
        p for p in sorted(base.rglob("*.osm.pbf"))
        if not _should_skip_pbf(p, include_extracts=include_extracts)
    ]
    if only:
        needles = tuple(n.casefold() for n in only if n.strip())
        found = [p for p in found if any(n in str(p).casefold() for n in needles)]
    return found


def harvest_pbf(path: Path, store: StreetAliasStore) -> int:
    """Recorre tags (sin locations). Devuelve pares nuevos insertados."""
    import osmium

    before = store.pair_count()
    pending: list[tuple[str, str]] = []

    def flush() -> None:
        if not pending:
            return
        store.add_pairs(pending)
        pending.clear()

    processor = osmium.FileProcessor(str(path))
    for obj in processor:
        if not obj.is_node() and not obj.is_way():
            continue
        tags = obj.tags
        if not tags:
            continue
        names = alias_names_from_tags(tags)
        if len(names) < 2:
            continue
        pending.extend(pair_names(names))
        if len(pending) >= 4_000:
            flush()
    flush()
    store.commit()
    return max(0, store.pair_count() - before)


def harvest_pbf_dir(
    root: str | Path,
    store: StreetAliasStore,
    *,
    include_extracts: bool = False,
    only: tuple[str, ...] = (),
    force: bool = False,
    max_files: int | None = None,
    progress=None,
) -> HarvestStats:
    stats = HarvestStats()
    files = iter_pbfs(root, include_extracts=include_extracts, only=only)
    if max_files is not None:
        files = files[:max_files]
    for path in files:
        if not force and store.already_harvested(path):
            stats.skipped += 1
            if progress:
                progress("skip", path, 0)
            continue
        try:
            added = harvest_pbf(path, store)
        except Exception as exc:
            stats.errors.append(f"{path}: {exc}")
            if progress:
                progress("error", path, 0)
            continue
        store.mark_harvested(path, added)
        stats.files += 1
        stats.pairs += added
        if progress:
            progress("ok", path, added)
    return stats
