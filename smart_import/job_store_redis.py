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

from .jobs import ANALYZING, BUSY_STATUSES, GEOCODE_QUEUED, Job, safe_filename
from .execution import token_for, ExecutionLost
from .identity import owns

#: Cuanto dura la reserva del geocode si nadie la libera. Es la red de seguridad
#: para un worker que muere sin escribir el estado final: pasado este plazo el
#: job se puede volver a tomar. Mas corto que el geocode mas largo que vimos
#: seria peor que no tener lock.
LOCK_TTL_S = 30 * 60

#: Piso entre dos escrituras de avance del MISMO job. El geocode llama a
#: `save_progress` una vez por fila: sin esto, 50k direcciones son 50k escrituras
#: de red que no le sirven a nadie (la UI pollea cada 500 ms como mucho).
PROGRESS_INTERVAL_S = 1.0


#: Cuanto sobrevive el estado de un job que todavia esta encolado o
#: procesandose, por corta que sea la retencion configurada.
BUSY_TTL_FLOOR_S = 24 * 3600


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
        # Piso para los jobs EN VUELO. La retencion corta es para los
        # terminados: el usuario ya se llevo el resultado y el disco no tiene
        # por que seguir ocupado. Pero un job encolado no controla cuando lo
        # toman — depende de cuanta cola haya adelante y de cuantos workers esten
        # prendidos —, y si su estado vence mientras espera, el worker lo levanta
        # y no encuentra el job: desde afuera se ve como un import que
        # desaparecio sin que nadie lo borrara.
        #
        # Con esto, `SMART_IMPORT_JOB_TTL_HOURS=4` significa lo que uno espera
        # que signifique (los resultados no se acumulan) sin poner en riesgo lo
        # que todavia no se proceso.
        self._ttl_busy_s = max(self._ttl_s, BUSY_TTL_FLOOR_S)
        self._artifacts = artifacts
        self._lock_ttl_s = int(lock_ttl_s)
        self._progress_interval_s = float(progress_interval_s)
        self._ultimo_progreso: dict[str, float] = {}
        self._candado = threading.Lock()
        #: Quien tiene la reserva. Solo para poder mirarlo desde `redis-cli`
        #: cuando un job quedo trabado y hay que saber a que nodo mirarle el log.
        self._holder = f"{socket.gethostname()}:{os.getpid()}"

    # ---------------------------------------------------------- claves

    @property
    def client(self):
        """El cliente Redis, para quien necesite el MISMO destino que el estado.

        Lo usa el latido de flota: abrir una segunda conexion para escribir una
        clave cada 30 s solo agregaria otra cosa que se puede desconfigurar
        distinto (otro host, otra db) y fallar en silencio.
        """
        return self._client

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
        if limit <= 0:
            return []
        jobs = []
        offset = 0
        while len(jobs) < limit:
            ids = self._client.zrevrange(self._index, offset, offset + 99)
            if not ids:
                break
            for raw in ids:
                job = self.get(_text(raw))
                if job is not None and owns(job):
                    jobs.append(job)
                    if len(jobs) == limit:
                        break
            offset += len(ids)
        return jobs

    def __len__(self) -> int:
        """Cuantos jobs conoce el indice.

        Puede quedar por encima de los que existen de verdad entre que el TTL
        vence una clave y pasa el barrido. Es para `/health`, que quiere un
        numero, no un inventario.
        """
        return int(self._client.zcard(self._index) or 0)

    # ---------------------------------------------------------- escritura

    def _ttl_para(self, job: Job) -> int:
        """Cuanto vive el estado de este job.

        En vuelo, el piso; terminado, la retencion configurada. Como `save` se
        llama en cada cambio de estado, la transicion a terminal acorta el TTL
        sola: no hace falta barrer nada para que el job empiece a caducar.
        """
        return self._ttl_busy_s if job.busy else self._ttl_s

    def create(self, filename: str, schema: str) -> Job:
        job = Job(id=f"imp_{uuid.uuid4().hex[:12]}",
                  filename=safe_filename(filename), schema=schema)
        self._client.set(self._key(job.id), json.dumps(job.as_state()),
                         ex=self._ttl_para(job))
        self._client.zadd(self._index, {job.id: job.created_at})
        return job

    def save(self, job: Job) -> Job:
        """Persiste el estado. Un job borrado no revive.

        El `xx=True` es lo que da esa garantia: si el barrido o un `DELETE` se
        llevaron el job mientras el worker trabajaba, la escritura tardia no
        crea la clave de nuevo — que seria un job sin archivos, ofreciendo
        botones de descarga que devuelven 409.
        """
        token = token_for(job.id)
        escrito = self._client.eval(SAVE_STATE, 3, self._key(job.id),
                                    self._run_key(job.id), self._lock_key(job.id),
                                    token or "", json.dumps(job.as_state()),
                                    self._ttl_para(job), int(not job.busy))
        if escrito == -1:
            raise ExecutionLost("execution lease lost")
        if not escrito:
            return job
        self._client.zadd(self._index, {job.id: job.created_at})
        if not job.busy:
            # Termino: se libera la reserva para que se pueda reintentar.
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

    def claim_normalize(self, job_id: str) -> bool:
        return self._claim_operation(job_id, ANALYZING)

    def claim_geocode(self, job_id: str) -> bool:
        return self._claim_operation(job_id, GEOCODE_QUEUED)

    def _claim_operation(self, job_id: str, status: str) -> bool:
        return bool(self._client.eval(CLAIM_OPERATION, 2, self._key(job_id),
            self._lock_key(job_id), json.dumps(list(BUSY_STATUSES)), status,
            time.time(), self._ttl_busy_s, self._holder, self._lock_ttl_s))

    def claim_run(self, job_id: str, holder: str, ttl_s: float) -> bool:
        return bool(self._client.set(self._run_key(job_id), holder,
                                     nx=True, ex=max(1, int(ttl_s))))

    def owns_run(self, job_id: str, holder: str) -> bool:
        return _text(self._client.get(self._run_key(job_id))) == holder

    def renew_run(self, job_id: str, holder: str, ttl_s: float) -> bool:
        return bool(self._client.eval(RENEW_RUN, 1, self._run_key(job_id),
                                     holder, max(1, int(ttl_s))))

    def release_run(self, job_id: str, holder: str) -> None:
        self._client.eval(RELEASE_RUN, 1, self._run_key(job_id), holder)

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


SAVE_STATE = """-- smart-import:save-state
if not redis.call('GET', KEYS[1]) then return 0 end
if ARGV[1] ~= '' and redis.call('GET', KEYS[2]) ~= ARGV[1] then return -1 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
if ARGV[4] == '1' then redis.call('DEL', KEYS[3]) end
return 1
"""
CLAIM_OPERATION = """-- smart-import:claim-operation
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local job = cjson.decode(raw)
for _, status in ipairs(cjson.decode(ARGV[1])) do
  if job.status == status then return 0 end
end
if not redis.call('SET', KEYS[2], ARGV[5], 'NX', 'EX', ARGV[6]) then return 0 end
job.status = ARGV[2]
job.updated_at = tonumber(ARGV[3])
redis.call('SET', KEYS[1], cjson.encode(job), 'EX', ARGV[4])
return 1
"""
RENEW_RUN = """-- smart-import:renew-run
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
RELEASE_RUN = """-- smart-import:release-run
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
