"""Un Redis de mentira, con lo justo que usa el store.

Deliberadamente NO es `fakeredis`: la regla de esta migracion es que ninguna
fase le agregue una dependencia de infraestructura a la suite. Lo que hay que
simular es poco y son las partes que importan — la atomicidad de `SET NX`, que
`SET XX` no cree la clave, y que las claves vencen — asi que un dict con un
candado alcanza y se lee en un minuto.

Es thread-safe a proposito: el test que prueba que solo un request se lleva la
reserva del geocode larga ocho hilos contra la misma clave.
"""
from __future__ import annotations

import threading
import time
from typing import Any


class FakeRedis:
    def __init__(self) -> None:
        self._valores: dict[str, tuple[Any, float | None]] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._candado = threading.RLock()
        #: Cuantas veces se escribio cada clave. Lo mira el test del throttle de
        #: progreso: la propiedad que interesa es "cuantas escrituras", no "que
        #: quedo guardado".
        self.escrituras: dict[str, int] = {}

    # ------------------------------------------------------------ interno

    def _vivo(self, key: str) -> bool:
        entrada = self._valores.get(key)
        if entrada is None:
            return False
        _, vence = entrada
        if vence is not None and vence <= time.time():
            del self._valores[key]
            return False
        return True

    def vencer(self, key: str) -> None:
        """Fuerza el vencimiento de una clave, sin esperar el TTL."""
        with self._candado:
            self._valores.pop(key, None)

    # ------------------------------------------------------------ strings

    def get(self, key: str):
        with self._candado:
            return self._valores[key][0] if self._vivo(key) else None

    def set(self, key: str, value, ex: float | None = None,
            nx: bool = False, xx: bool = False):
        with self._candado:
            existe = self._vivo(key)
            if nx and existe:
                return None
            if xx and not existe:
                return None
            self._valores[key] = (value, time.time() + ex if ex else None)
            self.escrituras[key] = self.escrituras.get(key, 0) + 1
            return True

    def delete(self, *keys: str) -> int:
        with self._candado:
            borradas = 0
            for key in keys:
                if self._vivo(key):
                    borradas += 1
                self._valores.pop(key, None)
            return borradas

    # ------------------------------------------------------------ zsets

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        with self._candado:
            zset = self._zsets.setdefault(key, {})
            nuevos = sum(1 for m in mapping if m not in zset)
            zset.update(mapping)
            return nuevos

    def _ordenados(self, key: str) -> list[str]:
        return [m for m, _ in sorted(self._zsets.get(key, {}).items(),
                                     key=lambda kv: (kv[1], kv[0]))]

    def zrange(self, key: str, start: int, end: int) -> list[str]:
        with self._candado:
            miembros = self._ordenados(key)
            return miembros[start:] if end == -1 else miembros[start:end + 1]

    def zrevrange(self, key: str, start: int, end: int) -> list[str]:
        with self._candado:
            miembros = list(reversed(self._ordenados(key)))
            return miembros[start:] if end == -1 else miembros[start:end + 1]

    def zrem(self, key: str, *miembros: str) -> int:
        with self._candado:
            zset = self._zsets.get(key, {})
            return sum(1 for m in miembros if zset.pop(m, None) is not None)

    def zcard(self, key: str) -> int:
        with self._candado:
            return len(self._zsets.get(key, {}))
