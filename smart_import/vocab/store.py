"""SQLite de conceptos + aliases. Offline, sin servicio.

Identidad = `key` (`package.box`, `status.pending`). El archivo se GENERA:
`python -m smart_import.vocab setup`. Si no hay sqlite, el runtime construye
el mismo modelo en memoria desde el catálogo empaquetado.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path

from ..resources import fold

SCHEMA = """
CREATE TABLE IF NOT EXISTS concepts (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL,
    canonical TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aliases (
    id INTEGER PRIMARY KEY,
    concept_id INTEGER NOT NULL REFERENCES concepts(id),
    alias TEXT NOT NULL,
    language TEXT,
    source TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    UNIQUE(concept_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_aliases_fold ON aliases(alias);
CREATE INDEX IF NOT EXISTS idx_concepts_domain ON concepts(domain);
CREATE INDEX IF NOT EXISTS idx_concepts_code ON concepts(domain, code);
"""

_DEFAULT_REL = Path("data/vocab/smart_import_vocab.sqlite")
_PACKAGED = Path(__file__).resolve().parent.parent / "resources" / "vocab" / "smart_import_vocab.sqlite"


def default_sqlite_path() -> Path:
    override = os.getenv("SMART_IMPORT_VOCAB_PATH")
    if override:
        return Path(override)
    return Path.cwd() / _DEFAULT_REL


class VocabularyStore:
    """Una conexion cacheada. Starlette corre POST /imports en un threadpool;
    el store suele abrirse antes en el thread principal (parse/mapping).
    Sin check_same_thread=False eso tira ProgrammingError y el import 422.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.conn.executescript(SCHEMA)

    def _fetchall(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def _fetchone(self, sql: str, params: tuple = ()):
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    @classmethod
    def open(cls, path: str | Path, *, create: bool = True) -> "VocabularyStore":
        path = Path(path)
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        return cls(conn)

    @classmethod
    def memory(cls) -> "VocabularyStore":
        return cls(sqlite3.connect(":memory:", check_same_thread=False))

    @classmethod
    def try_open(cls, path: str | Path | None = None) -> "VocabularyStore | None":
        target = Path(path) if path else default_sqlite_path()
        if not target.exists():
            return None
        return cls.open(target, create=False)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def schema_ok(self) -> bool:
        rows = self._fetchall("PRAGMA table_info(concepts)")
        return any(r["name"] == "key" for r in rows)

    def upsert_concept(
        self,
        key: str,
        domain: str,
        canonical: str,
        *,
        code: str | None = None,
        source: str,
    ) -> int:
        code = (code or "").strip()
        row = self._fetchone(
            """INSERT INTO concepts(key, domain, canonical, code, source)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 canonical=excluded.canonical,
                 code=CASE WHEN excluded.code != '' THEN excluded.code ELSE concepts.code END,
                 source=excluded.source
               RETURNING id""",
            (key, domain, canonical, code, source),
        )
        return int(row[0])

    def add_alias(
        self,
        concept_id: int,
        alias: str,
        *,
        language: str | None = None,
        source: str,
        weight: float = 1.0,
    ) -> None:
        raw = (alias or "").strip()
        if not raw:
            return
        with self._lock:
            self.conn.execute(
                """INSERT OR IGNORE INTO aliases(concept_id, alias, language, source, weight)
                   VALUES (?, ?, ?, ?, ?)""",
                (concept_id, raw, language, source, weight),
            )

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()

    def concept_id_by_key(self, key: str) -> int | None:
        row = self._fetchone("SELECT id FROM concepts WHERE key = ?", (key,))
        return int(row[0]) if row else None

    def concept_id_by_code(self, domain: str, code: str) -> int | None:
        folded = fold(code)
        if not folded:
            return None
        rows = self._fetchall(
            """SELECT id, code FROM concepts
               WHERE domain = ? AND code != ''""",
            (domain,),
        )
        for r in rows:
            if fold(r["code"]) == folded:
                return int(r["id"])
        return None

    def concept_id_by_alias(self, domain: str, alias: str) -> int | None:
        folded = fold(alias)
        if not folded:
            return None
        rows = self._fetchall(
            """SELECT a.concept_id, a.alias FROM aliases a
               JOIN concepts c ON c.id = a.concept_id
               WHERE c.domain = ?""",
            (domain,),
        )
        for r in rows:
            if fold(r["alias"]) == folded:
                return int(r["concept_id"])
        return None

    def resolve(self, alias: str, domain: str | None = None) -> dict | None:
        """Alias o código → concepto canónico (`package.box`, code `BX`)."""
        folded = fold(alias)
        if not folded:
            return None
        sql = """SELECT c.key, c.domain, c.canonical, c.code, a.alias
                 FROM aliases a
                 JOIN concepts c ON c.id = a.concept_id"""
        params: tuple = ()
        if domain:
            sql += " WHERE c.domain = ?"
            params = (domain,)
        for r in self._fetchall(sql, params):
            if fold(r["alias"]) == folded or (r["code"] and fold(r["code"]) == folded):
                return {
                    "key": r["key"],
                    "domain": r["domain"],
                    "canonical": r["canonical"],
                    "code": r["code"] or "",
                }
        return None

    def alias_map(self, domain: str, *, words_only: bool = False) -> dict[str, dict]:
        """alias foldeado → {canonical, code, key}, en UNA sola query.

        `resolve()` recorre la tabla por alias; para compilar un lexico entero
        (269 alias de packaging) eso es cuadratico. Aca se arma el diccionario
        completo de una pasada y el que compila lo cachea.
        """
        rows = self._fetchall(
            """SELECT a.alias, c.key, c.canonical, c.code FROM aliases a
               JOIN concepts c ON c.id = a.concept_id
               WHERE c.domain = ?""",
            (domain,),
        )
        out: dict[str, dict] = {}
        for r in rows:
            folded = fold(r["alias"])
            if not folded:
                continue
            code = r["code"] or ""
            if words_only and (_is_standard_code(r["alias"])
                               or (code and folded == fold(code))):
                continue
            out.setdefault(folded, {"key": r["key"], "canonical": r["canonical"],
                                    "code": code})
        return out

    def aliases(
        self,
        domain: str,
        *,
        codes_only: bool = False,
        words_only: bool = False,
    ) -> frozenset[str]:
        rows = self._fetchall(
            """SELECT a.alias, c.code FROM aliases a
               JOIN concepts c ON c.id = a.concept_id
               WHERE c.domain = ?""",
            (domain,),
        )
        out: set[str] = set()
        for alias, code in rows:
            folded = fold(alias)
            is_code = _is_standard_code(alias) or (
                code and fold(alias) == fold(code)
            )
            if codes_only and not is_code:
                continue
            if words_only and is_code:
                continue
            out.add(folded)
        return frozenset(out)

    def codes(self, domain: str) -> frozenset[str]:
        rows = self._fetchall(
            "SELECT code FROM concepts WHERE domain=? AND code IS NOT NULL AND code != ''",
            (domain,),
        )
        return frozenset(fold(r[0]) for r in rows if r[0] and str(r[0]).strip())

    def stats(self) -> dict[str, int | list[str]]:
        concepts = self._fetchone("SELECT COUNT(*) FROM concepts")[0]
        aliases = self._fetchone("SELECT COUNT(*) FROM aliases")[0]
        langs = [
            r[0]
            for r in self._fetchall(
                "SELECT DISTINCT language FROM aliases WHERE language IS NOT NULL ORDER BY 1"
            )
        ]
        by_domain = {
            r[0]: r[1]
            for r in self._fetchall(
                "SELECT domain, COUNT(*) FROM concepts GROUP BY domain"
            )
        }
        return {
            "concepts": concepts,
            "aliases": aliases,
            "languages": langs,
            "packaging_concepts": by_domain.get("packaging", 0),
            "status_concepts": by_domain.get("status", 0),
            "address_concepts": by_domain.get("address", 0),
        }

    def address_expand(self) -> dict[str, str]:
        """abbr → forma larga, para address_abbreviations()."""
        rows = self._fetchall(
            """SELECT a.alias, c.canonical FROM aliases a
               JOIN concepts c ON c.id = a.concept_id
               WHERE c.domain = 'address'"""
        )
        out: dict[str, str] = {}
        for alias, canonical in rows:
            key = fold(alias)
            if key and key != fold(canonical):
                out.setdefault(key, canonical)
        return out


def _is_standard_code(alias: str) -> bool:
    s = (alias or "").strip()
    return 2 <= len(s) <= 3 and s.isalpha() and s.isupper()


def reset_store_cache() -> None:
    get_store.cache_clear()


@lru_cache(maxsize=1)
def get_store() -> VocabularyStore:
    """Runtime: sqlite en disco si existe; si no, catálogo en memoria."""
    env = os.getenv("SMART_IMPORT_VOCAB_PATH")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env))
    else:
        candidates.append(Path.cwd() / _DEFAULT_REL)
    candidates.append(_PACKAGED)
    seen: set[Path] = set()
    for path in candidates:
        resolved = path if path.is_absolute() else Path.cwd() / path
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            store = VocabularyStore.open(resolved, create=False)
            if store.schema_ok():
                return store
            store.close()
            try:
                from .builder import build
                return build(resolved)
            except OSError:
                continue
    from .builder import build
    return build(dest=None)
