"""El loop del worker: sacar tareas de la cola y no perder ninguna.

Las decisiones que definen el comportamiento del nodo, todas juntas:

**Cuantas a la vez.** `worker_slots` threads y prefetch igual a los slots. El
prefetch NUNCA supera los slots: un mensaje entregado que espera a que se
libere un thread corre igual contra el `consumer_timeout` del broker, sin que
nadie lo este trabajando. Threads y no procesos porque el handler recibe un
contexto vivo (config, store, artefactos) que no se serializa, y porque es la
misma economia que ya tiene la API hoy; el paralelismo de CPU se consigue
prendiendo mas nodos, que es justamente el punto de todo esto.

**Que se reintenta.** Solo lo que puede andar mejor la proxima: un timeout
contra Redis, la api reiniciandose. Un archivo corrupto (`NormalizeFailed`) o
un job que ya no existe se confirman y se olvidan — reintentar diez veces un
.xlsx roto no lo arregla, y mientras tanto ocupa un slot. El reintento se
publica con demora creciente y un intento mas en el sobre; pasado el tope, la
tarea va a la DLQ con el motivo escrito y el job queda en `failed`, que es lo
que la UI le muestra al usuario en vez de un spinner eterno.

**Nunca dos veces a la vez.** La cola es at-least-once y redeliverea tambien
cuando se corta un canal, no solo cuando muere un nodo. Antes de trabajar, el
worker toma el lock de ejecucion del job (`claim_run`), que vence solo y se
renueva mientras dura la tarea; si lo tiene otro, la tarea se difiere con
demora en lugar de correr en paralelo y pisar la salida a medio escribir.

**El acuse va al final.** Siempre despues de publicar el resultado. Si el nodo
muere en el medio, el broker redeliverea y se rehace: se pierde tiempo, nunca
datos.

**Al bajar.** SIGTERM corta el consumo, deja terminar lo que ya empezo (hasta
`shutdown_drain_s`) y devuelve a la cola lo que llegue mientras tanto. Lo que
no llegue a terminar no se ackea, asi que lo toma otro nodo.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import uuid
from ..execution import Execution, ExecutionLost, executing
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as esperar_futures
from dataclasses import dataclass, field
from typing import Callable

from ..worker.handlers import NormalizeFailed, WorkerContext
from ..worker.tasks import JobDesaparecido, run_task
from .broker import Broker, Delivery, QueueNames
from .envelope import GEOCODE, NORMALIZE, InvalidTask, Task

#: El lock de ejecucion NO mide cuanto puede tardar un job: mientras el nodo
#: respira lo renueva cada `TTL/3`, asi que una tarea de una hora lo sostiene
#: sin problema. Lo que mide es **cuanto se espera antes de dar por muerto a un
#: nodo callado**, y por lo tanto cuanto tarda otro en retomar su trabajo.
#:
#: Bajarlo acelera el failover y sube el riesgo de una muerte falsa: si una
#: pausa de GC, un hipo de Redis o una laptop que se suspende dejan al nodo sin
#: renovar mas de un TTL, otro toma el job y el mismo archivo se procesa dos
#: veces. Por eso el piso: debajo de esto el jitter normal de red alcanza para
#: perder el lock estando vivo.
RUN_LOCK_TTL_MIN_S = 10.0
DIVISOR_RENOVACION = 3

#: Espera antes de reintentar, por numero de intento: 5 s, 10 s, 20 s… hasta un
#: minuto. Da tiempo a que se reinicie lo que se cayo sin dejar la tarea
#: parada media hora.
DEMORA_BASE_S = 5.0
DEMORA_MAX_S = 60.0

#: Cuanto espera una tarea diferida porque otro nodo tiene el job. Se deriva
#: del TTL del lock —no tiene sentido volver a preguntar mucho mas seguido de
#: lo que el lock puede tardar en vencer— con un piso para no encuestar en
#: vano. Es el sumando que se le agrega al TTL para saber cuanto tarda, en el
#: peor caso, otro nodo en retomar el trabajo de uno muerto.
DEMORA_OCUPADO_MIN_S = 5.0


def demora_para(intento: int) -> float:
    return min(DEMORA_BASE_S * (2 ** max(0, intento - 1)), DEMORA_MAX_S)


@dataclass
class EnVuelo:
    """Una tarea que se esta ejecutando ahora en este nodo."""

    task: Task
    receipt: str
    vence_en: float
    resuelta: bool = False
    future: Future | None = field(default=None, compare=False)
    execution: Execution | None = field(default=None, compare=False)


class Consumer:
    def __init__(self, cfg, broker: Broker, ctx_factory: Callable[[], WorkerContext],
                 logger: logging.Logger | None = None, pool=None):
        self.cfg = cfg
        self.broker = broker
        self._ctx_factory = ctx_factory
        self.log = logger or logging.getLogger(__name__)
        self.names = QueueNames(cfg.queue_prefix)
        self.slots = max(1, int(cfg.worker_slots))
        #: Quien es este nodo. Se escribe en el lock para que, cuando un job
        #: quede trabado, `redis-cli get …lock:run:<job>` diga a que maquina
        #: mirarle el log.
        self.holder = f"{socket.gethostname()}:{os.getpid()}"
        #: El piso no se negocia desde el entorno: un TTL de 2 s no acelera
        #: nada, solo hace que los nodos se roben el trabajo entre si.
        self.run_lock_ttl = max(RUN_LOCK_TTL_MIN_S, float(cfg.run_lock_ttl_s))
        self.demora_ocupado = max(DEMORA_OCUPADO_MIN_S,
                                  self.run_lock_ttl / DIVISOR_RENOVACION)

        self._pool = pool or ThreadPoolExecutor(max_workers=self.slots,
                                                thread_name_prefix="tarea")
        self._en_vuelo: dict[str, EnVuelo] = {}
        self._candado = threading.Lock()
        self._drenando = threading.Event()
        #: Receipts de tareas que se pasaron del tope y cuyo thread SIGUE
        #: corriendo. Es un conjunto y no un contador porque lo que se quiere
        #: saber es cuantos slots estan tomados ahora: una tarea que se paso de
        #: tiempo pero despues termina devuelve su slot, y no puede seguir
        #: contando para siempre contra la salud del nodo.
        self._colgadas: set[str] = set()

    # ---------------------------------------------------------- colas

    @property
    def colas(self) -> list[str]:
        """De cuales consume este nodo.

        Un nodo sin PBF montado no se suscribe a la de geocode: si lo hiciera,
        tomaria tareas que solo puede fallar, y la cola rebotaria entre nodos
        hasta la DLQ mientras el que si puede hacerlas mira sin trabajo.
        """
        colas = []
        if self.cfg.consume_normalize:
            colas.append(self.names.work(NORMALIZE))
        if self.cfg.consume_geocode:
            colas.append(self.names.work(GEOCODE))
        return colas

    def timeout_de(self, tipo: str) -> float:
        return float(self.cfg.task_timeout_geocode_s if tipo == GEOCODE
                     else self.cfg.task_timeout_normalize_s)

    # ---------------------------------------------------------- ciclo

    def run(self) -> None:
        """Consume hasta recibir una señal. Bloquea."""
        self._instalar_señales()
        vigia = threading.Thread(target=self._vigilar, name="vigia", daemon=True)
        vigia.start()
        self.log.info("worker %s listo — colas: %s, slots: %s",
                      self.holder, ", ".join(self.colas) or "(ninguna)", self.slots)
        try:
            self.broker.consume(self.colas, self.slots, self.on_message)
        finally:
            self._drenar()

    def stop(self) -> None:
        """Empieza la bajada. Idempotente: dos SIGTERM no rompen el drenaje."""
        if not self._drenando.is_set():
            self.log.info("bajando: no se toman tareas nuevas")
            self._drenando.set()
            self.broker.stop()

    def _instalar_señales(self) -> None:
        def _bajar(signum, _frame):
            self.log.info("señal %s recibida", signum)
            self.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _bajar)
            except ValueError:                   # pragma: no cover
                # No estamos en el thread principal (tests, embebido): quien nos
                # instancio se encarga de llamar a stop().
                pass

    def _drenar(self) -> None:
        with self._candado:
            pendientes = [v.future for v in self._en_vuelo.values()
                          if v.future is not None and not v.resuelta]
        if pendientes:
            self.log.info("drenando %s tarea(s) (hasta %ss)", len(pendientes),
                          self.cfg.shutdown_drain_s)
            esperar_futures(pendientes, timeout=float(self.cfg.shutdown_drain_s))
        # Lo que no llego a terminar queda sin ackear: el broker lo redeliverea
        # a otro nodo. No se cancela nada a mano; cancelar un thread no existe.
        self._pool.shutdown(wait=False)
        self.broker.close()

    # ---------------------------------------------------------- recepcion

    def on_message(self, entrega: Delivery) -> None:
        """Corre en el thread del loop de IO: decide rapido y no bloquea."""
        try:
            task = Task.from_bytes(entrega.body)
        except InvalidTask as exc:
            # No se reencola: el mensaje va a estar igual de malformado la
            # proxima vez. A la DLQ, donde alguien lo puede mirar.
            self.log.error("mensaje ilegible en %s → DLQ: %s", entrega.queue, exc)
            self.broker.nack(entrega.receipt, requeue=False)
            return

        if self._drenando.is_set():
            # Llego mientras bajabamos: devolverlo enseguida es mejor que
            # empezarlo para abandonarlo a la mitad.
            self.broker.nack(entrega.receipt, requeue=True)
            return

        vuelo = EnVuelo(task=task, receipt=entrega.receipt,
                        vence_en=time.monotonic() + self.timeout_de(task.type))
        with self._candado:
            self._en_vuelo[entrega.receipt] = vuelo
        vuelo.future = self._pool.submit(self._ejecutar, vuelo)

    # ---------------------------------------------------------- ejecucion

    def _ejecutar(self, vuelo: EnVuelo) -> None:
        task = vuelo.task
        ctx = self._ctx_factory()
        execution = Execution(task.job_id, uuid.uuid4().hex)
        vuelo.execution = execution
        if not ctx.store.claim_run(task.job_id, execution.token, self.run_lock_ttl):
            self.log.info("%s lo esta ejecutando otro nodo; se difiere", task)
            self._diferir(vuelo)
            return

        renovador = _Renovador(ctx.store, task.job_id, execution.token, self.log,
                               self.run_lock_ttl, execution.cancelled)
        renovador.start()
        try:
            # Todo lo que el worker baje o escriba es una copia de paso: al
            # salir de aca se borra, salga bien o mal. Sin esto, un nodo de
            # larga vida acumula el disco de todos los jobs que proceso.
            with executing(execution), ctx.artifacts.trabajando_en(task.job_id):
                run_task(ctx, task)
            self._resolver(vuelo, self.broker.ack, vuelo.receipt)
            self.log.info("listo: %s", task)
        except ExecutionLost:
            self.log.warning("execution lost for %s", task.job_id)
            if not vuelo.resuelta:
                self._diferir(vuelo)
        except JobDesaparecido:
            self.log.info("%s: el job ya no existe, nada que hacer", task)
            self._resolver(vuelo, self.broker.ack, vuelo.receipt)
        except (NormalizeFailed, InvalidTask) as exc:
            # Falla de dominio: el handler ya dejo el job en `failed` con el
            # motivo. Reintentar el mismo archivo roto no cambia nada.
            self.log.warning("%s no se puede hacer: %s", task, type(exc).__name__)
            if isinstance(exc, InvalidTask):
                self._marcar_fallido(task, "INVALID_TASK", execution)
            self._resolver(vuelo, self.broker.ack, vuelo.receipt)
        except Exception as exc:
            self.log.error("%s fallo: %s", task, type(exc).__name__)
            if not vuelo.resuelta:
                self._reintentar(vuelo, type(exc).__name__)
        finally:
            renovador.stop()
            ctx.store.release_run(task.job_id, execution.token)
            # Si esta tarea habia vencido y aun asi termino, su slot vuelve a
            # estar libre: dejarla contada haria que el nodo se apagara solo
            # despues de unas pocas tareas lentas repartidas en horas.
            with self._candado:
                self._colgadas.discard(vuelo.receipt)

    def _diferir(self, vuelo: EnVuelo) -> None:
        """Vuelve a la cola con demora en vez de correr en paralelo.

        No se nackea con requeue: con prefetch, un mensaje devuelto vuelve a
        entregarse al instante y el nodo entra en un ciclo cerrado consumiendo
        CPU para no hacer nada. Republicar con demora lo saca del camino.

        Y no cuenta como intento fallido. Si contara, un geocode largo que se
        entrego dos veces gastaria el presupuesto de reintentos mientras el
        original AVANZA BIEN, y al agotarlo marcaria como fallido un job que en
        ese momento esta por terminar. El techo aca es el tiempo: una tarea que
        lleva dando vueltas mas de lo que puede durar su propio trabajo ya no
        es un duplicado, es un mensaje huerfano.
        """
        task = vuelo.task
        edad = time.time() - task.created_at
        if edad > self.timeout_de(task.type) + self.run_lock_ttl:
            # A la DLQ SIN tocar el job: lo esta haciendo otro, y marcarlo
            # fallido desde aca seria pisar trabajo bueno con un error falso.
            self.log.error("%s lleva %.0fs esperando su turno: se aparta", task, edad)
            self.broker.send_to_dlq(task, f"esperando el turno hace {edad:.0f}s")
            self._resolver(vuelo, self.broker.ack, vuelo.receipt)
            return
        self.broker.publish(task, delay_s=self.demora_ocupado)
        self._resolver(vuelo, self.broker.ack, vuelo.receipt)

    def _reintentar(self, vuelo: EnVuelo, motivo: str,
                    demora: float | None = None) -> None:
        task = vuelo.task
        if task.attempt >= int(self.cfg.max_requeue_attempts):
            self._a_la_dlq(vuelo, f"{motivo} (tope de {task.attempt} intentos)")
            return
        siguiente = task.reintento(motivo)
        espera = demora if demora is not None else demora_para(task.attempt)
        # Se publica ANTES de ackear: si el proceso muere en el medio, el
        # broker redeliverea el original y a lo sumo se hace dos veces, que es
        # lo que la idempotencia de los handlers ya soporta. Al reves, se
        # perderia la tarea.
        self.broker.publish(siguiente, delay_s=espera)
        self._resolver(vuelo, self.broker.ack, vuelo.receipt)
        self.log.warning("%s se reintenta en %ss (intento %s): %s", task, espera,
                         siguiente.attempt, motivo)

    def _a_la_dlq(self, vuelo: EnVuelo, motivo: str) -> None:
        """Aparta la tarea. El orden no es casual.

        Primero se marca el job y recien despues se ackea. Al reves —que era
        como estaba— si marcar fallaba (Redis con un hipo, justo cuando la
        tarea agoto los reintentos) el mensaje ya estaba ackeado y en la DLQ,
        que no tiene consumidor: nadie iba a volver a intentarlo y el job
        quedaba `busy` para siempre, con el usuario mirando un spinner.

        Marcando primero, si eso falla tampoco se ackea, y el broker redeliverea
        la tarea: se reintenta el apartado entero, que es lo unico que puede
        destrabar al job.
        """
        self._marcar_fallido(vuelo.task, motivo, vuelo.execution)
        self.broker.send_to_dlq(vuelo.task, motivo)
        self._resolver(vuelo, self.broker.ack, vuelo.receipt)

    def _marcar_fallido(self, task: Task, motivo: str, execution=None) -> None:
        """Que el usuario vea el fracaso, en vez de un job ocupado para siempre."""
        from ..jobs import FAILED, GEOCODE_FAILED

        if execution is not None:
            # Also fence watchdog/error writes; never fail a newer attempt.
            try:
                with executing(Execution(task.job_id, execution.token)):
                    return self._marcar_fallido(task, motivo)
            except ExecutionLost:
                return

        try:
            ctx = self._ctx_factory()
            job = ctx.store.get(task.job_id)
            if job is None or not job.busy or (job.operation_task_id and job.operation_task_id != task.task_id):
                return
            job.error = motivo
            # Un geocode que fracasa no invalida el normalize: el archivo sigue
            # siendo descargable y la UI ofrece ubicar a mano.
            job.touch(GEOCODE_FAILED if task.type == GEOCODE else FAILED)
            ctx.store.save(job)
        except ExecutionLost:
            raise
        except Exception:
            # NO se traga: sin esta marca el job queda ocupado para siempre, y
            # dejar que suba es lo que evita el ack y deja que el broker lo
            # redeliverea para reintentar el apartado.
            self.log.exception("no se pudo marcar %s como fallido", task.job_id)
            raise

    def _resolver(self, vuelo: EnVuelo, acuse: Callable, receipt: str) -> None:
        """Acusa una sola vez. El vigia y el thread de la tarea compiten por esto."""
        with self._candado:
            if vuelo.resuelta:
                return
            vuelo.resuelta = True
            self._en_vuelo.pop(receipt, None)
        acuse(receipt)

    # ---------------------------------------------------------- vigia

    def _vigilar(self) -> None:
        """Aparta las tareas que se pasaron de tiempo.

        Un thread no se puede matar: la tarea colgada sigue ocupando su slot
        hasta que el proceso muera. Lo que si se puede es dejar de mentirle al
        usuario —el job pasa a fallido con el motivo— y devolver el mensaje,
        que si no quedaria sin ackear hasta que el broker corte la conexion.

        No se reintenta en otro nodo a proposito: si algo tardo mas que el tope,
        lo mas probable es que alla tambien cuelgue, y encima estaria corriendo
        junto con el zombi de aca sobre los mismos archivos. Va a la DLQ.

        Cuando se pierden todos los slots, este nodo ya no puede hacer nada:
        corta el consumo para que el supervisor lo reinicie y el broker
        reparta lo que quedo.
        """
        while not self._drenando.is_set():
            time.sleep(1.0)
            self.revisar_vencidas()

    def revisar_vencidas(self) -> int:
        """Aparta lo que se paso de tiempo. Devuelve cuantas encontro."""
        ahora = time.monotonic()
        with self._candado:
            vencidas = [v for v in self._en_vuelo.values()
                        if not v.resuelta and v.vence_en <= ahora]
        for vuelo in vencidas:
            if vuelo.execution:
                vuelo.execution.cancelled.set()
            limite = self.timeout_de(vuelo.task.type)
            self.log.error("%s supero los %ss y sigue corriendo: se aparta",
                           vuelo.task, limite)
            self._a_la_dlq(vuelo, f"la tarea supero los {limite:.0f}s")
            with self._candado:
                self._colgadas.add(vuelo.receipt)
        with self._candado:
            colgadas = len(self._colgadas)
        if vencidas and colgadas >= self.slots:
            self.log.error("los %s slots quedaron colgados; se corta el "
                           "consumo para que reinicien este nodo", self.slots)
            self.stop()
        return len(vencidas)


class _Renovador(threading.Thread):
    """Mantiene vivo el lock de ejecucion mientras la tarea trabaja."""

    def __init__(self, store, job_id: str, holder: str, log: logging.Logger,
                 ttl: float, cancelled: threading.Event | None = None):
        super().__init__(name=f"renueva-{job_id}", daemon=True)
        self._store = store
        self._job_id = job_id
        self._holder = holder
        self._log = log
        self._ttl = ttl
        self._cancelled = cancelled or threading.Event()
        self._fin = threading.Event()

    def run(self) -> None:
        while not self._fin.wait(self._ttl / DIVISOR_RENOVACION):
            try:
                if not self._store.renew_run(self._job_id, self._holder,
                                             self._ttl):
                    # El hilo puede seguir calculando; la barrera de estado y
                    # los artefactos por intento impiden que publique tarde.
                    self._cancelled.set()
                    self._log.warning(
                        "se perdio el lock de %s: otro nodo lo dio por muerto "
                        "(sin renovar por mas de %.0fs). Se descartan sus escrituras tardias",
                        self._job_id, self._ttl)
                    return
            except Exception as exc:                            # pragma: no cover
                self._log.warning("no se pudo renovar el lock de %s: %s",
                                  self._job_id, exc)

    def stop(self) -> None:
        self._fin.set()
