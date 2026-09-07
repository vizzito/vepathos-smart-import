"""Cache persistente de geocoding.

Los clientes de logistica repiten destinos: la misma direccion aparece semana a
semana. La clave es la direccion NORMALIZADA + el contexto regional, para que
'Av. Corrientes 1234' y 'AV CORRIENTES 1234' compartan entrada.

Es SQLite a proposito: sin dependencias nuevas y portable. La interfaz permite
cambiarlo por Redis/Postgres mas adelante sin tocar el geocoder.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
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
GEOCODER_VERSION = 5

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
    version INTEGER NOT NULL DEFAULT 0
);
"""


def make_key(address: str, context: str = "") -> str:
    payload = f"{normalize_text(address)}|{context}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class GeocodeCache:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()
        self.hits = 0
        self.misses = 0
        self.stale = 0

    def _migrate(self) -> None:
        """Agrega `version` a un cache creado antes de que existiera la columna."""
        columnas = {row[1] for row in self._conn.execute("PRAGMA table_info(geocode_cache)")}
        if "version" not in columnas:
            self._conn.execute(
                "ALTER TABLE geocode_cache ADD COLUMN version INTEGER NOT NULL DEFAULT 0")

    def get(self, address: str, context: str = "") -> GeocodeResult | None:
        row = self._conn.execute(
            "SELECT lat, lon, status, confidence, precision, source, normalized_address,"
            " version FROM geocode_cache WHERE key = ?", (make_key(address, context),)
        ).fetchone()
        if not row:
            self.misses += 1
            return None
        if row[7] != GEOCODER_VERSION:
            # entrada de una version anterior del geocoder: se ignora y se
            # vuelve a consultar; el resultado nuevo la pisa
            self.stale += 1
            self.misses += 1
            return None
        self.hits += 1
        return GeocodeResult(
            status=row[2], lat=row[0], lon=row[1], confidence=row[3] or 0.0,
            precision=row[4], source=row[5], normalized_address=row[6],
            detail={"from_cache": True},
        )

    def put(self, address: str, result: GeocodeResult, context: str = "") -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO geocode_cache"
            " (key, normalized_address, lat, lon, status, confidence, precision, source,"
            "  created_at, version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (make_key(address, context), result.normalized_address or normalize_text(address),
             result.lat, result.lon, result.status, result.confidence,
             result.precision, result.source, time.time(), GEOCODER_VERSION),
        )

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses,
                "stale": self.stale,
                "hit_rate": round(self.hits / total, 3) if total else 0.0}
