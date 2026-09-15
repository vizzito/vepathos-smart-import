"""Cache persistente de geocoding.

Los clientes de logistica repiten destinos: la misma direccion aparece semana a
semana. La clave es la direccion con lo que el geocoder no distingue plegado
(ver `key_text`) + el contexto regional, para que 'Av. Corrientes 1234' y
'AV CORRIENTES 1234' compartan entrada.

Es SQLite a proposito: sin dependencias nuevas, portable, y el acceso queda
detras de esta interfaz por si alguna vez hay que cambiar el motor.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from pathlib import Path

from .address import normalize_text
from .base import GeocodeResult

#: Se sube cuando cambia la logica de matching (consulta, scoring, umbrales).
#: Las entradas de una version anterior se ignoran: si no, una mejora del
#: geocoder es INVISIBLE hasta borrar el cache a mano. Los `not_found` son los
#: que mas envenenan, porque una direccion que antes no se encontraba se sigue
#: reportando como perdida aunque ahora si se encuentre.
#: Bump cuando cambia enrichment / geofence / scoring (invalida cache vieja).
#: v4: la clave de cache tambien incluye umbrales (ver runner context).
#: v7: veto de CP/localidad (homónimos en otra ciudad no pintan verde).
#: v9: query con ciudad + OSM sin addr:city = mismatch (Punta Arenas).
#: v10: vacío no veta (Toronto); distancia al destino (Radom/Holstebro).
#: v11: esponente 15/B ≡ 15B / 15 (no exigir token B en FTS).
#: v13: alias name:fr/nl (Avenue Mozart ≡ Mozartstraat) + FTS bilingue.
#: v14: rescates cuando no hay pin (numero pegado, calle resuelta contra el indice,
#:      esquinas) + gate con evidencia del indice. Los not_found de v13 envenenan.
#: v15: el rescate guarda con que se valido (cruces de la esquina, calle resuelta):
#:      el runner lo re-valida con eso y no con la calle del texto crudo. La
#:      interpolacion no mezcla alturas de dos calles homonimas.
#: v16: la clave ya no junta consultas que el geocoder lee distinto ('4 Стрелча Sofia'
#:      heredaba el not_found de '4 Стрелча, Sofia') y el contexto identifica el
#:      indice sin mtime, que cambiaba en cada apertura (0 hits entre imports).
#: v17: una inicial busca y puntua contra el nombre completo ('Juan B Justo 4500' ≡
#:      'Avenida Juan Bautista Justo'): los not_found de v16 envenenan.
#: v18: solo la letra ENTRE dos palabras es inicial: v17 pintaba ambar a 4-8 km
#:      '16 Avenida B 0-26' (Guatemala) y dejaba sin pin 'Tebet Utara I'.
GEOCODER_VERSION = 18

SCHEMA = """
CREATE TABLE IF NOT EXISTS geocode_cache (
    key TEXT PRIMARY KEY,
    normalized_address TEXT NOT NULL,
    lat REAL, lon REAL,
    status TEXT NOT NULL,
    confidence REAL,
    precision TEXT,
    source TEXT,
    created_at REAL NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
"""


#: Punto que cierra una abreviatura ('Av. Corrientes'). El que queda pegado a lo
#: que sigue arma otra lectura: 'AV.1733' no encuentra lo que 'AV. 1733' si.
_CLOSING_PERIOD = re.compile(r"\.(?=\s|$)")
#: Hasta aca las letras latinas en las que NFD separa la tilde (Latin Extended-B).
_LATIN_END = chr(0x0250)


def key_text(address: str) -> str:
    """Texto de la clave: pliega SOLO lo que no cambia lo que lee el geocoder.

    Mayusculas, tildes, espacios repetidos y el punto de una abreviatura. El resto
    de la puntuacion queda tal cual porque el parser la usa: corta segmentos en
    ',' ';' '|' y arma alturas con '-', '/', '#', 'º'. `normalize_text` la borraba
    y juntaba consultas distintas en una entrada: '4 Стрелча Sofia' heredaba el
    not_found de '4 Стрелча, Sofia' y 'Ossington Ave - 38' el pin de 'Ossington
    Ave 38', o sea el resultado de una fila dependia de las filas anteriores.

    Tildes solo sobre letras latinas: en otras escrituras la marca combinante es
    parte de la letra ('が' sin ella es 'か'; en tailandes es la vocal). Y NFD, no
    el NFKD de `strip_accents`, que convierte 'Nº' en 'No': el parser reconoce la
    altura pegada a 'Nº' pero no a 'No'.

    Medido en las 82 suites de geocode-regression (cada consulta geocodificada
    sola, 2026-09-15): con `normalize_text` 215 consultas heredaban otro resultado;
    con esta clave 3, pins ambar a nivel calle en Guadalajara donde el propio
    geocoder elige otra calle homonima segun mayusculas o tildes.
    """
    kept: list[str] = []
    for ch in unicodedata.normalize("NFD", str(address)):
        if unicodedata.combining(ch) and kept and kept[-1] < _LATIN_END:
            continue
        kept.append(ch)
    return " ".join(_CLOSING_PERIOD.sub(" ", "".join(kept).lower()).split())


def make_key(address: str, context: str = "") -> str:
    payload = json.dumps([key_text(address), context]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


#: Segundos que un escritor espera al otro antes de fallar. El default de
#: sqlite3 son 5 s: poco margen cuando el otro job esta escribiendo su fila.
BUSY_TIMEOUT_S = 30.0


class GeocodeCache:
    """Cache compartida entre jobs concurrentes.

    Dos decisiones que parecen detalles y son la diferencia entre que
    SMART_IMPORT_GEOCODE_WORKERS=2 funcione o mate el segundo job:

    * **WAL**: los lectores no se bloquean con el escritor.
    * **autocommit** (`isolation_level=None`): cada escritura es su propia
      transaccion, corta. Antes se acumulaba todo hasta un unico commit al
      final del archivo, asi que la primera fila tomaba el lock de escritura y
      no lo soltaba nunca: el segundo job esperaba el timeout y moria con
      "database is locked". Agrupar en lotes tampoco alcanza — entre fila y
      fila hay una consulta al indice OSM, y el lote retiene el lock todo ese
      tiempo.
    """

    def __init__(self, path: str | Path, *, ttl_s: float = 30 * 86400,
                 negative_ttl_s: float = 86400, max_entries: int = 500_000):
        self.ttl_s = ttl_s
        self.negative_ttl_s = negative_ttl_s
        self.max_entries = max_entries
        self._writes = 0
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        try:
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:
                pass
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.execute("CREATE INDEX IF NOT EXISTS cache_age ON geocode_cache(created_at)")
            self.purge()
        except BaseException:
            self._conn.close()
            raise
        self.hits = 0
        self.misses = 0
        self.stale = 0

    def _migrate(self) -> None:
        """Agrega `version` a un cache creado antes de que existiera la columna."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(geocode_cache)")}
            for name, declaration in (("version", "INTEGER NOT NULL DEFAULT 0"),
                                      ("detail_json", "TEXT NOT NULL DEFAULT '{}'")):
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE geocode_cache ADD COLUMN {name} {declaration}")
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def purge(self) -> None:
        now = time.time()
        self._conn.execute(
            "DELETE FROM geocode_cache WHERE version != ? OR created_at < ? "
            "OR (status = 'not_found' AND created_at < ?)",
            (GEOCODER_VERSION, now - self.ttl_s, now - self.negative_ttl_s))
        self._conn.execute(
            "DELETE FROM geocode_cache WHERE key IN (SELECT key FROM geocode_cache "
            "ORDER BY created_at DESC LIMIT -1 OFFSET ?)", (self.max_entries,))

    def get(self, address: str, context: str = "") -> GeocodeResult | None:
        row = self._conn.execute(
            "SELECT lat, lon, status, confidence, precision, source, normalized_address,"
            " version, created_at, detail_json FROM geocode_cache WHERE key = ?", (make_key(address, context),)
        ).fetchone()
        if not row:
            self.misses += 1
            return None
        ttl = self.negative_ttl_s if row[2] == "not_found" else self.ttl_s
        if row[7] != GEOCODER_VERSION or time.time() - row[8] >= ttl:
            # entrada de una version anterior del geocoder: se ignora y se
            # vuelve a consultar; el resultado nuevo la pisa
            self.stale += 1
            self.misses += 1
            return None
        try:
            detail = json.loads(row[9])
            if not isinstance(detail, dict):
                raise ValueError("Invalid cached detail")
        except (ValueError, TypeError):
            self.misses += 1
            return None
        self.hits += 1
        return GeocodeResult(
            status=row[2], lat=row[0], lon=row[1], confidence=row[3] or 0.0,
            precision=row[4], source=row[5], normalized_address=row[6],
            detail={**detail, "from_cache": True},
        )

    def put(self, address: str, result: GeocodeResult, context: str = "") -> None:
        if result is None or result.status == "error":
            return
        self._writes += 1
        if self._writes % 1000 == 0:
            self.purge()
        self._conn.execute(
            "INSERT OR REPLACE INTO geocode_cache"
            " (key, normalized_address, lat, lon, status, confidence, precision, source,"
            "  created_at, version, detail_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (make_key(address, context), result.normalized_address or normalize_text(address),
             result.lat, result.lon, result.status, result.confidence,
             result.precision, result.source, time.time(), GEOCODER_VERSION,
             json.dumps({k: v for k, v in (result.detail or {}).items()
                         if k in {"soft_reject", "raw_score", "reason", "street",
                                  "intersection", "rescue_names"}}, allow_nan=False)),
        )
    def commit(self) -> None:
        """No-op: en autocommit cada `put` ya quedo escrito.

        Se conserva porque el runner lo llama al terminar el archivo, y porque
        deja el contrato listo si algun dia la cache vuelve a agrupar.
        """

    def close(self) -> None:
        self._conn.close()

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses,
                "stale": self.stale,
                "hit_rate": round(self.hits / total, 3) if total else 0.0}
