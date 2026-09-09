"""Almacen de jobs: metadata y archivos, con dos backends posibles.

Este modulo es el de MEMORIA: un dict con un candado, que es el despliegue de un
solo proceso (`SMART_IMPORT_ROLE=embedded`). Sigue siendo el default y sigue sin
necesitar infraestructura, con la consecuencia de siempre — el estado no
sobrevive a un reinicio, y cada instancia solo conoce sus propios jobs.

Con el trabajo repartido entre maquinas ese dict no alcanza, y el estado se
guarda en Redis (`job_store_redis.py`) detras de esta misma interfaz. La
diferencia que importa al leer el codigo de arriba: alla `get()` devuelve una
COPIA, asi que mutar el `Job` NO alcanza para que el cambio se vea. Toda
escritura tiene que terminar en `save()` o `save_progress()`, incluso aca donde
hoy es casi un no-op — es lo unico que hace que cambiar de backend sea
configuracion y no una caceria de estados que no avanzan.
"""
from __future__ import annotations

import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

#: Version del formato de `as_state()`. No se usa para migrar (el estado es
#: efimero: dura horas y se regenera solo), pero queda escrito para poder
#: reconocer de que version viene un job al mirar Redis a mano.
STATE_VERSION = 1

# estados del job (los mismos que va a usar la integracion con routehub)
UPLOADED = "uploaded"
ANALYZING = "analyzing"
NEEDS_REVIEW = "needs_mapping_review"
NORMALIZED = "normalized"          # estado FINAL valido aunque nunca se geocodifique
GEOCODE_QUEUED = "geocode_queued"
GEOCODING = "geocoding"
GEOCODE_FAILED = "geocode_failed"  # normalize OK; geocode duro fallo → UI pide geo manual
COMPLETED = "completed"
FAILED = "failed"

# operaciones largas: la web pollea / se suscribe hasta que busy=false
BUSY_STATUSES = {UPLOADED, ANALYZING, GEOCODE_QUEUED, GEOCODING}

# Normalize sobrevivio: se puede descargar y (re)intentar geocode / geo manual
HAS_NORMALIZE = {NORMALIZED, NEEDS_REVIEW, COMPLETED, GEOCODE_FAILED}

ALLOWED_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls", ".json"}
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str | None) -> str:
    """El nombre lo elige el usuario: nunca se usa crudo para armar una ruta."""
    base = Path(name or "upload").name
    cleaned = _UNSAFE.sub("_", base).strip("._-") or "upload"
    return cleaned[:120]


@dataclass
class Job:
    id: str
    filename: str
    status: str = UPLOADED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    schema: str = "vepathos_flat_v1"
    phone_region: str | None = None
    timezone: str | None = None
    raw_path: str | None = None
    normalized_path: str | None = None
    nested_path: str | None = None
    geocoded_path: str | None = None
    report: dict = field(default_factory=dict)
    geocode_report: dict = field(default_factory=dict)
    geocode_progress: dict = field(default_factory=dict)
    error: str | None = None
    # marca de tiempo del inicio de la operacion async actual (para eta)
    op_started_at: float | None = None
    #: Que tarea produjo el resultado que hoy tiene el job.
    #:
    #: La cola es at-least-once: un mensaje se redeliverea si se corta el canal
    #: antes del acuse, aunque el trabajo haya terminado perfecto. Sin esto, ese
    #: mensaje repetido vuelve a normalizar y PISA lo que ya estaba — incluido
    #: el resultado de un geocode posterior, porque el normalize reescribe
    #: `report` entero y devuelve el estado a `normalized`.
    #:
    #: Guardar el id de la tarea que ya termino deja distinguir "esto hay que
    #: hacerlo" de "esto ya se hizo y lo que se perdio fue el acuse".
    normalize_task_id: str | None = None
    geocode_task_id: str | None = None

    def touch(self, status: str | None = None) -> None:
        if status:
            self.status = status
        self.updated_at = time.time()

    @property
    def needs_geocode(self) -> int:
        return int(self.report.get("needs_geocode", 0))

    @property
    def busy(self) -> bool:
        return self.status in BUSY_STATUSES

    #: capacidades del despliegue; las setea la API al arrancar
    capabilities: dict = field(default_factory=lambda: {"geocoding": True})

    def next_actions(self) -> list[dict[str, str]]:
        """Que puede hacer el usuario ahora.

        Solo se ofrece lo que este despliegue puede ejecutar de verdad: si el
        geocoding esta apagado, el boton no aparece en vez de fallar al apretarlo.
        Geocodificar SIEMPRE es opt-in. Mientras busy, no se ofrecen acciones.
        """
        if self.busy:
            return []
        actions: list[dict[str, str]] = []
        if self.status in HAS_NORMALIZE and self.normalized_path:
            actions.append({
                "action": "download",
                "href": f"/imports/{self.id}/download?format=flat",
                "description": "Descargar el archivo normalizado (formato Vepathos)",
            })
            if self.nested_path:
                actions.append({
                    "action": "download_nested",
                    "href": f"/imports/{self.id}/download?format=nested",
                    "description": "Descargar JSON nested (optimizador)",
                })
            if self.geocoded_path:
                actions.append({
                    "action": "download_geocoded",
                    "href": f"/imports/{self.id}/download?format=geocoded",
                    "description": "Descargar CSV geocodificado",
                })
        if self.status == NEEDS_REVIEW:
            actions.append({
                "action": "confirm_mapping",
                "href": f"/imports/{self.id}/mapping",
                "description": "Corregir el mapping de las columnas dudosas y re-normalizar",
            })
        # geocode_failed / completed con residuales: el normalize quedo; se puede
        # reintentar o ubicar a mano en la UI
        if (self.status in (NORMALIZED, NEEDS_REVIEW, GEOCODE_FAILED, COMPLETED)
                and self.needs_geocode
                and self.capabilities.get("geocoding", True)):
            actions.append({
                "action": "geocode",
                "href": f"/imports/{self.id}/geocode",
                "description": (
                    f"Geolocalizar {self.needs_geocode} fila(s) sin coordenadas. "
                    "Usa el address cargado (sin reescribirlo). "
                    "Opcional: ?enhance_addresses=true para mejorar solo la query."
                ),
                "options": {
                    "enhance_addresses": {
                        "type": "boolean",
                        "default": False,
                        "label": "Mejorar direcciones para geocodificar",
                        "help": (
                            "Reescribe la query interna (parser/enhance). "
                            "El address visible en la tabla no cambia."
                        ),
                    },
                },
            })
        if self.status == GEOCODE_FAILED or (
                self.status == COMPLETED and self.needs_geocode > 0):
            actions.append({
                "action": "manual_geocode",
                "href": f"/imports/{self.id}/issues",
                "description": (self.error
                                or f"{self.needs_geocode} entrega(s) sin coordenadas. "
                                   "Ubicalas a mano en el mapa."),
            })
        return actions

    def progress_snapshot(self) -> dict[str, Any]:
        """Vista unica de avance para la web (poll o SSE).

        Siempre presente en la respuesta del job. Mientras `busy=true`, la UI
        muestra barra / texto; cuando pasa a false, mira `next_actions`.
        """
        phase = "idle"
        done = 0
        total = 0
        detail: dict[str, Any] = {}
        message = ""

        if self.status in (GEOCODE_QUEUED, GEOCODING):
            p = self.geocode_progress or {}
            phase = str(p.get("phase") or self.status)
            done = int(p.get("done") or 0)
            total = int(p.get("total") or self.report.get("rows_output") or 0)
            detail = {k: v for k, v in p.items()
                      if k not in ("done", "total", "phase", "started_at")}
            if phase == "building_index":
                message = f"Construyendo indice OSM ({p.get('pbf', '')})"
            elif phase == "geocoding":
                message = f"Geolocalizando {done}/{total}"
            elif phase == "queued" or self.status == GEOCODE_QUEUED:
                message = "Geolocalizacion en cola"
            elif phase == "done":
                message = "Geolocalizacion lista"
            elif phase == "failed":
                message = self.error or "Geolocalizacion fallo"
        elif self.status == FAILED:
            phase = "failed"
            message = self.error or "Fallo"
        elif self.status == GEOCODE_FAILED:
            phase = "geocode_failed"
            message = (self.error
                       or "Geolocalizacion automatica fallo — ubica a mano en el mapa")
        elif self.status == NEEDS_REVIEW:
            phase = "needs_review"
            message = "Revisar mapping"
        elif self.status == COMPLETED:
            phase = "done"
            message = "Completado"
        elif self.status == NORMALIZED:
            phase = "normalized"
            message = "Normalizado"
        elif self.status in (UPLOADED, ANALYZING):
            # Con el trabajo repartido, esta ventana dura lo que tarde un worker
            # en tomar la tarea. Sin mensaje, la UI muestra una barra sin texto y
            # el usuario no sabe si esta pasando algo.
            phase = "queued" if self.status == UPLOADED else "analyzing"
            message = ("En cola" if self.status == UPLOADED
                       else "Procesando el archivo")

        pct = round(100.0 * done / total, 1) if total > 0 else None
        eta_s = None
        started = self.op_started_at
        if started and done > 0 and total > done and self.busy:
            rate = done / max(time.time() - started, 0.001)
            if rate > 0:
                eta_s = round((total - done) / rate, 1)

        return {
            "job_id": self.id,
            "status": self.status,
            "phase": phase,
            "message": message,
            "done": done,
            "total": total,
            "pct": pct,
            "eta_s": eta_s,
            "busy": self.busy,
            "detail": detail or None,
            "updated_at": self.updated_at,
            "error": self.error,
        }

    def poll_after_ms(self) -> int | None:
        """Cada cuanto conviene volver a preguntar. Crece con la espera.

        Un intervalo fijo de 500 ms es razonable para un normalize de 2 s, pero
        una extraccion con modelo puede tardar minutos: a 500 ms fijos son
        cientos de requests que no aportan nada. Se arranca rapido (la mayoria
        de los jobs terminan enseguida) y se va espaciando hasta 3 s.
        """
        if not self.busy:
            return None
        esperando = time.time() - self.updated_at
        if esperando < 5:
            return 500
        if esperando < 20:
            return 1_000
        if esperando < 60:
            return 2_000
        return 3_000

    def urls(self) -> dict[str, str]:
        base = f"/imports/{self.id}"
        return {
            "self": base,
            "events": f"{base}/events",
            "preview": f"{base}/preview",
            "download_flat": f"{base}/download?format=flat",
            "download_nested": f"{base}/download?format=nested",
            "download_geocoded": f"{base}/download?format=geocoded",
            "geocode": f"{base}/geocode",
            "mapping": f"{base}/mapping",
        }

    def as_state(self) -> dict[str, Any]:
        """El estado COMPLETO del job, para guardarlo fuera del proceso.

        No es `as_dict()`: eso es el contrato con la web (trae `next_actions`,
        `urls`, `progress` — todo derivado) y le FALTAN campos internos que
        ningun cliente mira pero de los que depende el trabajo, como
        `phone_region`, `op_started_at` o `capabilities`. Serializar la vista de
        la API y reconstruir desde ahi pierde justamente eso, en silencio.

        Se derivan de los campos del dataclass a proposito: agregar un campo
        nuevo queda guardado sin que haya que acordarse de tocar este metodo.
        """
        estado = {f.name: getattr(self, f.name) for f in fields(self)}
        estado["_version"] = STATE_VERSION
        return estado

    @classmethod
    def from_state(cls, estado: dict[str, Any]) -> "Job":
        """Reconstruye un job guardado.

        Tolerante en las dos direcciones, porque durante un deploy conviven dos
        versiones del codigo leyendo el mismo Redis: una clave que no conocemos
        se ignora (la escribio una version mas nueva) y una que falta toma el
        default del dataclass (la escribio una mas vieja).
        """
        validos = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in estado.items() if k in validos})

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "status": self.status,
            "busy": self.busy,
            "filename": self.filename,
            "schema": self.schema,
            "timezone": self.timezone,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "report": self.report,
            "progress": self.progress_snapshot(),
            "geocode": {**self.geocode_report, "progress": self.geocode_progress}
                       if (self.geocode_report or self.geocode_progress) else None,
            "error": self.error,
            "next_actions": self.next_actions(),
            "urls": self.urls(),
            # hint para la web: cada cuantos ms conviene pollear si no usa SSE
            "poll_after_ms": self.poll_after_ms(),
        }


class JobStore:
    def __init__(self, work_dir: str | Path):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        #: job_id → (quien lo ejecuta, cuando vence). Ver `claim_run`.
        self._corriendo: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def create(self, filename: str, schema: str) -> Job:
        job_id = f"imp_{uuid.uuid4().hex[:12]}"
        job = Job(id=job_id, filename=safe_filename(filename), schema=schema)
        with self._lock:
            self._jobs[job_id] = job
        (self.dir_for(job_id) / "raw").mkdir(parents=True, exist_ok=True)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def save(self, job: Job) -> Job:
        """Deja asentado el estado del job.

        En memoria es casi un no-op: quien muta un `Job` muta el mismo objeto que
        ven todos, asi que hoy nadie NECESITA llamar a esto. Esa comodidad es
        justamente lo que no sobrevive a un store remoto, donde `get()` devuelve
        una COPIA y una escritura sin `save` se pierde sin que falle nada: el job
        queda "geocodificando" para siempre y ningun log lo delata.

        Marcar cada escritura mientras el backend sigue siendo un dict es lo que
        convierte ese cambio en configuracion en vez de una caceria de bugs
        invisibles.

        Un job borrado no revive: si el barrido o un `DELETE` se lo llevaron
        mientras el worker trabajaba, la escritura tardia se descarta.
        """
        with self._lock:
            if job.id in self._jobs:
                self._jobs[job.id] = job
        return job

    def save_progress(self, job: Job) -> Job:
        """Como `save`, para las escrituras de avance.

        Se separa porque tienen otra economia: el geocode llama a esto UNA VEZ
        POR FILA, y contra un store remoto eso es una escritura de red por
        direccion. El backend distribuido las va a limitar (una por segundo por
        job); perder una intermedia no cuesta nada, porque la siguiente trae el
        estado completo igual.
        """
        return self.save(job)

    def claim_geocode(self, job_id: str) -> bool:
        """Reserva el job para geocodificar. False si ya esta ocupado.

        Mirar el estado y despues escribirlo son dos pasos: separados, dos POST
        simultaneos (doble click, retry del front, reintento de httpx) pasan los
        dos la guarda y lanzan dos workers sobre el mismo CSV, que se pisan el
        archivo de salida y el reporte. Aca el chequeo y el cambio ocurren bajo
        el mismo lock, asi que solo uno puede reservar.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in BUSY_STATUSES:
                return False
            job.touch(GEOCODE_QUEUED)
            return True

    def claim_run(self, job_id: str, holder: str, ttl_s: float) -> bool:
        """Toma el derecho a EJECUTAR una tarea de este job. False si lo tiene otro.

        Es distinto de `claim_geocode`, que es una reserva de negocio ("el
        usuario ya pidio geocodificar") y dura hasta que el job termina. Esta es
        de ejecucion y dura lo que dura la tarea: existe porque una cola
        at-least-once redeliverea, y dos workers normalizando el mismo job se
        pisan el archivo de salida a medio escribir.

        Vence sola y hay que renovarla mientras se trabaja: un worker que muere
        no deja el job trabado, y por eso `kill -9` termina con otro nodo
        retomando en vez de un job clavado hasta el TTL largo.
        """
        with self._lock:
            duenio, vence = self._corriendo.get(job_id, (None, 0.0))
            if duenio is not None and duenio != holder and vence > time.monotonic():
                return False
            self._corriendo[job_id] = (holder, time.monotonic() + ttl_s)
            return True

    def renew_run(self, job_id: str, holder: str, ttl_s: float) -> bool:
        with self._lock:
            duenio, _ = self._corriendo.get(job_id, (None, 0.0))
            if duenio != holder:
                return False
            self._corriendo[job_id] = (holder, time.monotonic() + ttl_s)
            return True

    def release_run(self, job_id: str, holder: str) -> None:
        with self._lock:
            duenio, _ = self._corriendo.get(job_id, (None, 0.0))
            if duenio == holder:
                self._corriendo.pop(job_id, None)

    def list(self, limit: int = 50) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: -j.created_at)[:limit]

    def __len__(self) -> int:
        """Cuantos jobs vivos hay. Para /health, que solo quiere el numero:
        contarlos con `list()` ordenaba todo el store en cada probe."""
        return len(self._jobs)

    def dir_for(self, job_id: str) -> Path:
        return self.work_dir / job_id

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        shutil.rmtree(self.dir_for(job_id), ignore_errors=True)
        return True

    def purge_older_than(self, max_age_s: float) -> list[str]:
        """Borra los jobs terminados que ya nadie va a mirar.

        No habia ni TTL ni limite: cada import dejaba raw + normalized +
        geocoded + reports en disco para siempre, y el dict en memoria nunca se
        podaba. En un host que comparte disco con el cutter eso termina en
        'no space left on device' a la madrugada.

        Un job ocupado nunca se toca, por viejo que parezca: puede ser un
        geocode largo construyendo un indice.
        """
        if max_age_s <= 0:
            return []
        corte = time.time() - max_age_s
        with self._lock:
            vencidos = [jid for jid, job in self._jobs.items()
                        if job.updated_at < corte and not job.busy]
        return [jid for jid in vencidos if self.delete(jid)]


def make_job_store(cfg, artifacts=None):
    """El almacen de estado de este proceso, segun el rol.

    `embedded` (el default) sigue siendo el dict en memoria: un solo proceso, sin
    infraestructura, exactamente el comportamiento de siempre. Los roles `api` y
    `worker` existen para repartirse el trabajo, y eso solo funciona si comparten
    el estado: un job creado por la api tiene que ser visible para el worker que
    lo procesa, y su avance para la api que lo sirve.

    Sin Redis no arranca, en vez de degradar a memoria: un rol `api` con un store
    local responde 404 para jobs que existen y que otro proceso esta procesando
    ahora mismo. Fallar al arrancar es mucho mas barato que ese sintoma.
    """
    if cfg.role == "embedded":
        return JobStore(cfg.work_dir)
    if not cfg.redis_host:
        raise ValueError(
            f"SMART_IMPORT_ROLE={cfg.role} reparte el trabajo entre procesos y "
            "necesita el estado compartido, pero REDIS_HOST esta vacio. "
            "Configuralo, o volve a SMART_IMPORT_ROLE=embedded.")
    from .job_store_redis import make_redis_job_store
    return make_redis_job_store(cfg, artifacts)
