"""El estado de los jobs, compartido entre procesos.

Misma interfaz que `JobStore`, otra propiedad: aca `get()` devuelve una COPIA
reconstruida desde Redis, no el objeto que muta el que escribe. Por eso cada
escritura tiene que pasar por `save()` — ver el docstring de `JobStore.save`, y
`tests/test_job_escrituras_explicitas.py`, que corre los endpoints contra un
store que copia justamente para que ese olvido falle en CI y no en produccion.

Lo que se guarda es metadata: kilobytes por job. Los archivos no van a Redis.

Tres claves por job, todas bajo el prefijo configurable:

    {prefijo}job:{job_id}            el estado completo, con TTL
    {prefijo}jobs                    ZSET por created_at, para `list()`
    {prefijo}lock:geocode:{job_id}   la reserva del geocode (la pide el usuario)
    {prefijo}lock:run:{job_id}       quien lo esta ejecutando AHORA (entre workers)

El lock es la version distribuida de `claim_geocode`: en memoria alcanzaba con
mirar y escribir el estado bajo el mismo `threading.Lock`, pero entre dos
procesos ese lock no existe y los dos pasan la guarda. `SET NX EX` la resuelve
donde si es atomico. Lo libera `save()` cuando el job deja de estar ocupado, para
que la propiedad siga siendo la misma que en memoria — un job que termino se
puede volver a reservar — sin que los handlers tengan que acordarse de nada.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from typing import Any

from .jobs import GEOCODE_QUEUED, Job, safe_filename

#: Cuanto dura la reserva del geocode si nadie la libera. Es la red de seguridad
#: para un worker que muere sin escribir el estado final: pasado este plazo el
#: job se puede volver a tomar. Mas corto que el geocode mas largo que vimos
#: seria peor que no tener lock.
LOCK_TTL_S = 30 * 60

#: Piso entre dos escrituras de avance del MISMO job. El geocode llama a
#: `save_progress` una vez por fila: sin esto, 50k direcciones son 50k escrituras
#: de red que no le sirven a nadie (la UI pollea cada 500 ms como mucho).
PROGRESS_INTERVAL_S = 1.0


def _text(raw: Any) -> str | None:
    """redis-py devuelve bytes salvo con `decode_responses`. Aceptamos los dos."""
    if raw is None:
        return None
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


class RedisJobStore:
    def __init__(self, client, *, prefix: str = "smartimport:",
                 ttl_s: float = 24 * 3600, artifacts=None,
                 lock_ttl_s: float = LOCK_TTL_S,
                 progress_interval_s: float = PROGRESS_INTERVAL_S):
        self._client = client
        self._prefix = prefix
        # El TTL de Redis va MAS LARGO que el del barrido a proposito: el
        # barrido es quien borra los archivos del disco, y si la metadata se
        # vence antes, esos archivos quedan huerfanos sin nadie que los nombre.
        self._ttl_s = max(1, int(ttl_s * 2))
        self._artifacts = artifacts
        self._lock_ttl_s = int(lock_ttl_s)
        self._progress_interval_s = float(progress_interval_s)
        self._ultimo_progreso: dict[str, float] = {}
        self._candado = threading.Lock()
        #: Quien tiene la reserva. Solo para poder mirarlo desde `redis-cli`
        #: cuando un job quedo trabado y hay que saber a que nodo mirarle el log.
        self._holder = f"{socket.gethostname()}:{os.getpid()}"

    # ---------------------------------------------------------- claves

    def _key(self, job_id: str) -> str:
        return f"{self._prefix}job:{job_id}"

    def _lock_key(self, job_id: str) -> str:
        return f"{self._prefix}lock:geocode:{job_id}"

    def _run_key(self, job_id: str) -> str:
        return f"{self._prefix}lock:run:{job_id}"

    @property
    def _index(self) -> str:
        return f"{self._prefix}jobs"

    # ---------------------------------------------------------- lectura

    def get(self, job_id: str) -> Job | None:
        crudo = _text(self._client.get(self._key(job_id)))
        if crudo is None:
            return None
        try:
            return Job.from_state(json.loads(crudo))
        except (ValueError, TypeError):
            # Un estado ilegible es un job perdido, no un servicio caido: la UI
            # ve un 404 y el usuario vuelve a subir el archivo.
            return None

    def list(self, limit: int = 50) -> list[Job]:
        ids = [_text(i) for i in self._client.zrevrange(self._index, 0, max(0, limit - 1))]
        jobs = [self.get(job_id) for job_id in ids if job_id]
        return [job for job in jobs if job is not None]

    def __len__(self) -> int:
        """Cuantos jobs conoce el indice.

        Puede quedar por encima de los que existen de verdad entre que el TTL
        vence una clave y pasa el barrido. Es para `/health`, que quiere un
        numero, no un inventario.
        """
        return int(self._client.zcard(self._index) or 0)

    # ---------------------------------------------------------- escritura

    def create(self, filename: str, schema: str) -> Job:
        job = Job(id=f"imp_{uuid.uuid4().hex[:12]}",
                  filename=safe_filename(filename), schema=schema)
        self._client.set(self._key(job.id), json.dumps(job.as_state()), ex=self._ttl_s)
        self._client.zadd(self._index, {job.id: job.created_at})
        return job

    def save(self, job: Job) -> Job:
        """Persiste el estado. Un job borrado no revive.

        El `xx=True` es lo que da esa garantia: si el barrido o un `DELETE` se
        llevaron el job mientras el worker trabajaba, la escritura tardia no
        crea la clave de nuevo — que seria un job sin archivos, ofreciendo
        botones de descarga que devuelven 409.
        """
        escrito = self._client.set(self._key(job.id), json.dumps(job.as_state()),
                                   ex=self._ttl_s, xx=True)
        if not escrito:
            return job
        self._client.zadd(self._index, {job.id: job.created_at})
        if not job.busy:
            # Termino: se libera la reserva para que se pueda reintentar.
            self._client.delete(self._lock_key(job.id))
            with self._candado:
                self._ultimo_progreso.pop(job.id, None)
        return job

    def save_progress(self, job: Job) -> Job:
        """Avance, limitado a una escritura por segundo y por job.

        Perder una intermedia no cuesta nada: la siguiente trae el estado
        completo igual. Los cambios de estado NO pasan por aca (usan `save`),
        asi que nada terminal se puede perder por el limite.
        """
        ahora = time.monotonic()
        with self._candado:
            ultimo = self._ultimo_progreso.get(job.id)
            if ultimo is not None and (ahora - ultimo) < self._progress_interval_s:
                return job
            self._ultimo_progreso[job.id] = ahora
        return self.save(job)

    def claim_geocode(self, job_id: str) -> bool:
        """Reserva el job para geocodificar. False si ya esta ocupado.

        El chequeo de `busy` es el atajo barato; el que decide es el `SET NX`,
        que es la unica parte atomica entre procesos.
        """
        job = self.get(job_id)
        if job is None or job.busy:
            return False
        if not self._client.set(self._lock_key(job_id), self._holder,
                                nx=True, ex=self._lock_ttl_s):
            return False
        job.touch(GEOCODE_QUEUED)
        self.save(job)
        return True

    def claim_run(self, job_id: str, holder: str, ttl_s: float) -> bool:
        """Toma el derecho a EJECUTAR una tarea de este job. False si lo tiene otro.

        Es la exclusion entre WORKERS, distinta de `claim_geocode`, que es la
        reserva de negocio del usuario. La cola es at-least-once: un mensaje se
        redeliverea porque se corto un canal, no solo porque el nodo murio, y
        dos workers escribiendo la misma salida la dejan a medias.

        El TTL corto es lo que hace que un `kill -9` no clave el job: el que
        trabaja renueva, el que murio deja de renovar y en un minuto otro lo
        toma. Reclamar lo propio de nuevo (reintento en el mismo nodo) se
        permite, o un worker se bloquearia a si mismo.
        """
        clave = self._run_key(job_id)
        if self._client.set(clave, holder, nx=True, ex=max(1, int(ttl_s))):
            return True
        return _text(self._client.get(clave)) == holder and self.renew_run(
            job_id, holder, ttl_s)

    def renew_run(self, job_id: str, holder: str, ttl_s: float) -> bool:
        """Estira la reserva mientras se trabaja. False si ya no es nuestra.

        Leer y escribir son dos pasos: entre medio la clave puede vencer y
        tomarla otro, y esta renovacion se la robaria. La ventana es de
        microsegundos contra un TTL de minutos, y el costo de perderla es que
        dos nodos hagan el mismo trabajo idempotente — no vale un script Lua.
        """
        if _text(self._client.get(self._run_key(job_id))) != holder:
            return False
        return bool(self._client.set(self._run_key(job_id), holder,
                                     ex=max(1, int(ttl_s)), xx=True))

    def release_run(self, job_id: str, holder: str) -> None:
        if _text(self._client.get(self._run_key(job_id))) == holder:
            self._client.delete(self._run_key(job_id))

    def delete(self, job_id: str) -> bool:
        existia = bool(self._client.delete(self._key(job_id)))
        self._client.zrem(self._index, job_id)
        self._client.delete(self._lock_key(job_id))
        with self._candado:
            self._ultimo_progreso.pop(job_id, None)
        # Los archivos se barren aunque no hubiera metadata: sin ella nadie
        # puede nombrarlos, asi que lo unico que pueden hacer es ocupar disco.
        # Es el caso del job que vencio por TTL antes de que pasara el barrido.
        if self._artifacts is not None:
            self._artifacts.delete(job_id)
        return existia

    def purge_older_than(self, max_age_s: float) -> list[str]:
        """Borra los jobs terminados que ya nadie va a mirar.

        Un job ocupado nunca se toca, por viejo que parezca: puede ser un
        geocode largo construyendo un indice.

        Ademas limpia lo que dejo vencer el TTL de Redis: sin metadata nadie
        sabe que esos archivos existen, asi que se van con el indice.
        """
        if max_age_s <= 0:
            return []
        corte = time.time() - max_age_s
        borrados: list[str] = []
        for crudo in self._client.zrange(self._index, 0, -1):
            job_id = _text(crudo)
            if not job_id:
                continue
            job = self.get(job_id)
            if job is None:
                self.delete(job_id)
                continue
            if job.updated_at < corte and not job.busy and self.delete(job_id):
                borrados.append(job_id)
        return borrados


def make_redis_job_store(cfg, artifacts=None) -> RedisJobStore:
    """Cliente de Redis + store. El import es perezoso: `redis` es un extra."""
    try:
        import redis
    except ImportError as exc:                                  # pragma: no cover
        raise RuntimeError(
            f"SMART_IMPORT_ROLE={cfg.role} necesita Redis para compartir el estado "
            "entre procesos, pero el paquete no esta instalado. "
            "Instalalo con el extra: pip install -e '.[queue]'") from exc

    client = redis.Redis(
        host=cfg.redis_host, port=cfg.redis_port,
        password=cfg.redis_password or None, db=cfg.redis_db,
        decode_responses=True,
        # Que un Redis caido se note como error del request y no como un proceso
        # colgado: sin timeouts, el threadpool se llena de hilos esperando.
        socket_timeout=5, socket_connect_timeout=5, health_check_interval=30,
    )
    return RedisJobStore(client, prefix=cfg.redis_prefix,
                         ttl_s=float(cfg.job_ttl_hours) * 3600,
                         artifacts=artifacts)
