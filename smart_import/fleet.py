"""Quien esta trabajando ahora mismo, y si estan todos de acuerdo.

Cuando el trabajo se reparte entre maquinas aparecen dos preguntas que en un solo
proceso no existian: *quien hay* y *estan configurados igual*. Las dos se
contestan con lo mismo — cada worker publica una clave en Redis con TTL corto y
la refresca mientras vive.

**Quien hay.** El TTL es el interruptor: un worker que se cae deja de refrescar y
desaparece solo, sin que nadie lo tenga que dar de baja. Es el mismo mecanismo
que usa el cutter del optimizer (`graph_cutter:alive`), y es lo que hace que
prender y apagar un worker sea `docker compose up` / `down` y nada mas.

**Estan de acuerdo.** Este es el problema silencioso. El worker ESTAMPA las
bandas de geocode en el CSV y la api las REPINTA para la UI leyendo su propio
`Config` (ver el "Siempre recalcular" de `issues`). Si los dos no tienen los
mismos umbrales, el operador ve verde donde el archivo dice ambar, y no hay
ningun error en ningun lado: solo un pin en el que confia de mas. Por eso el
latido lleva una huella de esa config y `/health` marca al que no coincide.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any

#: Campos cuyo valor TIENE que coincidir entre la api y todos los workers.
#:
#: No es toda la config: `pbf_dir` o `worker_slots` son legitimamente distintos
#: en cada maquina. Son los que definen que sale en el archivo y como se pinta,
#: o sea donde una diferencia produce un resultado incorrecto en vez de una
#: diferencia de capacidad.
CAMPOS_COMPARTIDOS = (
    "geocode_valid_band",
    "geocode_review_band",
    "match_threshold",
    "low_confidence_threshold",
    "geocode_street_level_floor",
    "geocode_street_match_min",
    "geocode_soft_reject",
    "geocode_soft_reject_min",
    "max_file_mb",
    "max_rows",
    "default_schema",
    #: libpostal cambia COMO se parsea una direccion, no que puede hacer el
    #: nodo. Un worker con libpostal y otro sin el resuelven la misma fila a
    #: pines distintos, y eso no se nota por ningun otro lado: no hay error, no
    #: hay log, el archivo simplemente sale distinto segun quien lo agarro.
    #:
    #: Es distinto de `pbf_dir`, que si es legitimamente propio de cada maquina:
    #: ese decide QUE regiones puede servir un nodo (capacidad), no que resultado
    #: da para una region que ya puede servir.
    "libpostal_enabled",
)


def config_digest(cfg) -> str:
    """Huella corta de la config que api y workers tienen que compartir.

    Corta a proposito: no sirve para auditar, sirve para responder "sos igual a
    mi, si o no". Un digest largo en `/health` no se lee; uno de 12 caracteres se
    compara de un vistazo entre dos maquinas.
    """
    material = json.dumps(
        {campo: getattr(cfg, campo, None) for campo in CAMPOS_COMPARTIDOS},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def node_id() -> str:
    """Identidad del proceso. Igual que el cutter: maquina + pid."""
    return f"{os.uname().nodename}:{os.getpid()}"


def _key(prefix: str, nodo: str) -> str:
    return f"{prefix}node:{nodo}"


class Heartbeat:
    """Anuncia este worker mientras viva. Best-effort, nunca lo tumba.

    Si Redis no contesta, el nodo se apaga solo en `/health` cuando vence el TTL
    — pero el worker sigue consumiendo tareas, porque el latido es telemetria,
    no una condicion para trabajar. Al reves seria peor: un hipo de Redis dejaria
    la flota entera sin procesar.
    """

    def __init__(self, client, cfg, *, colas: list[str], logger: logging.Logger | None = None):
        self._client = client
        self._cfg = cfg
        self._colas = list(colas)
        self._logger = logger or logging.getLogger(__name__)
        self._prefix = cfg.redis_prefix
        self._ttl_s = max(15, int(cfg.node_heartbeat_ttl_s))
        # Un tercio del TTL: entran dos latidos fallidos antes de que el nodo
        # desaparezca, asi un hipo de red no lo hace parpadear en la UI.
        self._periodo_s = max(5, self._ttl_s // 3)
        self._nodo = node_id()
        self._stop = threading.Event()
        self._hilo: threading.Thread | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "node": self._nodo,
            "role": self._cfg.role,
            "queues": self._colas,
            "slots": self._cfg.worker_slots,
            "config_digest": config_digest(self._cfg),
            "geocoding": bool(self._cfg.geocoding_enabled and self._cfg.pbf_dir),
            "version": "0.1.0",
            "seen_at": time.time(),
        }

    def beat(self) -> bool:
        """Un latido. True si quedo asentado."""
        try:
            self._client.set(_key(self._prefix, self._nodo),
                             json.dumps(self.payload()), ex=self._ttl_s)
            return True
        except Exception as exc:
            self._logger.warning("latido fallido (%s); se reintenta en %ss",
                                 exc, self._periodo_s)
            return False

    def start(self) -> "Heartbeat":
        if self._hilo is not None:
            return self
        self.beat()   # el primero inmediato: el nodo aparece al arrancar, no en 30s
        self._hilo = threading.Thread(target=self._loop, name="fleet-heartbeat",
                                      daemon=True)
        self._hilo.start()
        self._logger.info("latido cada %ss como %s", self._periodo_s, self._nodo)
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self._periodo_s):
            self.beat()

    def stop(self) -> None:
        """Baja el nodo YA, sin esperar el TTL.

        Un `docker compose down` limpio no tiene por que dejar un fantasma 90
        segundos en `/health`.
        """
        self._stop.set()
        try:
            self._client.delete(_key(self._prefix, self._nodo))
        except Exception:
            pass   # el TTL lo resuelve igual


def read_fleet(client, prefix: str) -> list[dict[str, Any]]:
    """Los workers vivos, ordenados por nombre.

    Un `scan` + un solo `mget` en vez de un `get` por clave: contra un Redis al
    otro lado de un tunel, la diferencia entre una llamada y N es la que se ve.
    """
    try:
        claves = sorted(client.scan_iter(match=f"{prefix}node:*", count=1000))
    except Exception:
        return []
    if not claves:
        return []
    nodos = []
    for crudo in client.mget(claves):
        if not crudo:
            continue
        try:
            nodos.append(json.loads(crudo))
        except (ValueError, TypeError):
            continue   # un nodo con estado ilegible no rompe el /health de todos
    return sorted(nodos, key=lambda n: str(n.get("node", "")))


def drift(nodos: list[dict[str, Any]], digest_api: str) -> list[str]:
    """Los nodos cuya config no coincide con la de la api.

    Vacio es lo normal. Con algo adentro, ese worker esta estampando bandas con
    otros umbrales que los que la api usa para pintarlas.
    """
    return [str(n.get("node", "?")) for n in nodos
            if n.get("config_digest") and n["config_digest"] != digest_api]
