"""El transporte de las tareas. RabbitMQ, con la topologia explicita.

El contrato es chico a proposito —publicar, consumir, ack/nack— porque tiene dos
implementaciones que importan: esta, y el `FakeBroker` de los tests, que es lo
que permite probar reintentos, DLQ y drenaje sin levantar un broker en CI.

Topologia (exchange por defecto, routing key = nombre de cola; sin plugins):

    smart-import-normalize          trabajo. DLX → dlq
    smart-import-geocode            idem, separada para que un nodo elija que
                                    consume: el geocode necesita PBF montado
    smart-import-<tipo>-delay       sin consumidor. Cada mensaje trae su TTL y
                                    al vencer el DLX lo devuelve a su cola
    smart-import-dlq                lo que fracaso definitivamente, con TTL de
                                    24 h para que no crezca sin techo

El delay se hace con TTL por mensaje + dead-letter y no con el plugin de mensajes
demorados: es la misma tecnica que usa el optimizer contra este mismo broker, no
necesita plugins instalados y funciona en cualquier RabbitMQ.

Convenciones que NO son decorativas y valen para las dos implementaciones:

  - `BlockingConnection` no es thread-safe. El loop de consumo vive en el thread
    principal, asi que TODO lo que un worker haga desde otro thread (ack, nack,
    publicar un reintento) pasa por `add_callback_threadsafe`, y las
    publicaciones usan una conexion aparte.
  - El acuse lleva la EPOCA del canal. Despues de una reconexion, el broker ya
    redeliverio lo que estaba sin ackear: un ack tardio con el delivery tag
    viejo confirmaria el mensaje de otro. Con la epoca, se descarta.
"""
from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Callable

from .envelope import GEOCODE, NORMALIZE, Task

#: Cuanto sobrevive un mensaje en la DLQ. Sin esto, la cola de lo que fracaso
#: crece para siempre y termina siendo el problema en vez del sintoma.
DLQ_TTL_MS = 24 * 3600 * 1000

#: Parametros de conexion, calcados de los que ya usa el optimizer contra este
#: mismo RabbitMQ: un heartbeat largo tolera una tarea que no cede el thread, y
#: los reintentos de conexion cubren el arranque simultaneo con el broker.
HEARTBEAT_S = 600
BLOCKED_TIMEOUT_S = 300
CONNECTION_ATTEMPTS = 3
RETRY_DELAY_S = 5


@dataclass(frozen=True)
class Delivery:
    """Un mensaje entregado, todavia sin ackear."""

    body: bytes
    receipt: str
    queue: str


class Broker(ABC):
    """Lo minimo que el consumidor necesita del transporte."""

    @abstractmethod
    def declare(self) -> None:
        """Crea la topologia. Idempotente: se llama en cada (re)conexion."""

    @abstractmethod
    def publish(self, task: Task, *, delay_s: float = 0.0) -> None:
        """Encola la tarea. Con `delay_s`, se entrega recien despues de ese rato."""

    @abstractmethod
    def send_to_dlq(self, task: Task, motivo: str) -> None:
        """Aparta la tarea con el motivo escrito, para que alguien la mire."""

    @abstractmethod
    def consume(self, colas: list[str], prefetch: int,
                on_message: Callable[[Delivery], None]) -> None:
        """Bloquea consumiendo hasta que alguien llame a `stop()`."""

    @abstractmethod
    def ack(self, receipt: str) -> None:
        """Confirma. Se puede llamar desde otro thread."""

    @abstractmethod
    def nack(self, receipt: str, *, requeue: bool) -> None:
        """Devuelve (`requeue=True`) o descarta hacia la DLQ. Desde otro thread."""

    @abstractmethod
    def depths(self) -> dict[str, int]:
        """Cuantos mensajes esperan en cada cola. `{}` si no se pudo averiguar.

        Es la unica senal de saturacion que queda cuando el trabajo se va a la
        cola: sin esto, una sobrecarga deja de verse como un 429 y pasa a ser
        latencia invisible, que es peor de diagnosticar.
        """
        return {}

    def stop(self) -> None:
        """Corta el consumo. `consume()` retorna. Se llama desde una señal."""

    def close(self) -> None:
        """Cierra lo que haya quedado abierto."""


class QueueNames:
    """Los nombres de las colas de un despliegue, derivados del prefijo."""

    def __init__(self, prefix: str = "smart-import") -> None:
        self.prefix = prefix.rstrip("-")
        self.dlq = f"{self.prefix}-dlq"

    def work(self, tipo: str) -> str:
        return f"{self.prefix}-{tipo}"

    def delay(self, tipo: str) -> str:
        return f"{self.prefix}-{tipo}-delay"

    @property
    def all_work(self) -> list[str]:
        return [self.work(NORMALIZE), self.work(GEOCODE)]


class RabbitBroker(Broker):
    def __init__(self, cfg, logger: logging.Logger | None = None,
                 *, intentos_conexion: int = CONNECTION_ATTEMPTS,
                 reintentos_publicacion: int = 3):
        self._cfg = cfg
        self._log = logger or logging.getLogger(__name__)
        self.names = QueueNames(cfg.queue_prefix)
        #: Cuanto se insiste con el broker. Un worker que arranca junto al
        #: broker puede darse el lujo de esperar; la API no: del otro lado del
        #: publish hay alguien mirando un spinner, y es mejor un 503 en cuatro
        #: segundos que un timeout de un minuto.
        self._intentos_conexion = max(1, intentos_conexion)
        self._reintentos_publicacion = max(1, reintentos_publicacion)

        self._conn = None
        self._channel = None
        #: Conexion aparte para publicar mientras se consume: pika prohibe tocar
        #: el canal del consumidor desde otro thread, y los reintentos se
        #: publican desde los threads que ejecutan las tareas.
        self._pub_conn = None
        self._pub_channel = None
        #: Serializa TODO acceso a un canal de pika. `BlockingConnection` no es
        #: thread-safe y en el rol api conviven dos escritores en el mismo
        #: canal: los publish de los endpoints y el `queue_declare(passive)` con
        #: el que la flota mide las colas. Sin esto, sus frames AMQP se
        #: entrelazan y el canal se cae sola cada tanto, sin patron aparente.
        self._pub_lock = threading.RLock()
        #: (medido_en, {cola: profundidad}) — ver DEPTH_CACHE_S
        self._depths_cache: tuple[float, dict[str, int]] | None = None

        self._consuming = False
        self._stop = threading.Event()
        self._tags: list[str] = []
        #: Se incrementa en cada canal nuevo. Un acuse de una epoca vieja se tira.
        self._epoca = 0

    # ---------------------------------------------------------- conexion

    def _params(self):
        import pika

        return pika.ConnectionParameters(
            host=self._cfg.rabbitmq_host,
            port=self._cfg.rabbitmq_port,
            virtual_host=self._cfg.rabbitmq_vhost,
            credentials=pika.PlainCredentials(self._cfg.rabbitmq_user,
                                              self._cfg.rabbitmq_password),
            heartbeat=HEARTBEAT_S,
            blocked_connection_timeout=BLOCKED_TIMEOUT_S,
            connection_attempts=self._intentos_conexion,
            retry_delay=RETRY_DELAY_S,
        )

    def _conectar(self):
        import pika

        if self._conn is not None and self._conn.is_open:
            if self._channel is not None and self._channel.is_open:
                return
        self._conn = pika.BlockingConnection(self._params())
        self._channel = self._conn.channel()
        # Confirmaciones del broker: un publish que "salio" pero no llego seria
        # una tarea perdida en silencio, y el usuario esperando para siempre.
        self._channel.confirm_delivery()
        self._epoca += 1
        self.declare()
        self._log.info("conectado a RabbitMQ %s:%s", self._cfg.rabbitmq_host,
                       self._cfg.rabbitmq_port)

    def _canal_publicacion(self):
        """El canal por el que se publica. El caller YA tiene `_pub_lock`.

        Consumiendo, es una conexion aparte: pika prohibe tocar el canal del
        consumidor desde otro thread. Sin consumir —el rol api— es el canal
        principal, que no tiene a nadie mas escribiendo... salvo la medicion de
        colas, y por eso ese caso tambien entra por aca con el lock tomado.
        """
        if not self._consuming:
            self._conectar()
            return self._channel
        import pika

        if self._pub_conn is None or not self._pub_conn.is_open:
            self._pub_conn = pika.BlockingConnection(self._params())
            self._pub_channel = self._pub_conn.channel()
            self._pub_channel.confirm_delivery()
        return self._pub_channel

    # ---------------------------------------------------------- topologia

    def declare(self) -> None:
        canal = self._channel
        canal.queue_declare(queue=self.names.dlq, durable=True,
                            arguments={"x-message-ttl": DLQ_TTL_MS})
        for tipo in (NORMALIZE, GEOCODE):
            canal.queue_declare(
                queue=self.names.work(tipo), durable=True,
                arguments={"x-dead-letter-exchange": "",
                           "x-dead-letter-routing-key": self.names.dlq})
            # La de espera no tiene consumidor: los mensajes vencen y el DLX los
            # devuelve a la cola de trabajo. Eso es el "delay".
            canal.queue_declare(
                queue=self.names.delay(tipo), durable=True,
                arguments={"x-dead-letter-exchange": "",
                           "x-dead-letter-routing-key": self.names.work(tipo)})

    # ---------------------------------------------------------- publicar

    def publish(self, task: Task, *, delay_s: float = 0.0) -> None:
        cola = (self.names.delay(task.type) if delay_s > 0
                else self.names.work(task.type))
        propiedades = {"delivery_mode": 2, "content_type": "application/json",
                       "message_id": task.task_id}
        if delay_s > 0:
            propiedades["expiration"] = str(int(delay_s * 1000))
        self._publicar(cola, task.as_bytes(), propiedades)

    def send_to_dlq(self, task: Task, motivo: str) -> None:
        # El motivo viaja DENTRO del mensaje: quien mire la DLQ dentro de una
        # semana no va a tener el log del nodo que la aparto.
        apartada = replace(task, error=motivo)
        self._publicar(self.names.dlq, apartada.as_bytes(),
                       {"delivery_mode": 2, "content_type": "application/json",
                        "message_id": task.task_id})
        self._log.error("a la DLQ: %s — %s", task, motivo)

    def _publicar(self, cola: str, cuerpo: bytes, propiedades: dict) -> None:
        import pika

        ultimo = None
        for intento in range(self._reintentos_publicacion):
            try:
                with self._pub_lock:
                    canal = self._canal_publicacion()
                    canal.basic_publish(
                        exchange="", routing_key=cola, body=cuerpo,
                        properties=pika.BasicProperties(**propiedades))
                return
            except Exception as exc:                            # pragma: no cover
                ultimo = exc
                self._log.warning("fallo publicando en %s (%s/%s): %s", cola,
                                  intento + 1, self._reintentos_publicacion, exc)
                self._reset_publicacion()
                time.sleep(1 + intento)
        raise RuntimeError(f"no se pudo publicar en {cola}: {ultimo}")

    def _reset_publicacion(self) -> None:                       # pragma: no cover
        """Tira la conexion para que la proxima reconecte. Con `_pub_lock` tomado."""
        with self._pub_lock:
            if self._consuming:
                self._pub_conn = None
                self._pub_channel = None
            else:
                self._conn = None
                self._channel = None

    # ---------------------------------------------------------- consumir

    def consume(self, colas: list[str], prefetch: int,
                on_message: Callable[[Delivery], None]) -> None:
        espera = 1
        while not self._stop.is_set():
            try:
                self._conectar()
                self._consuming = True
                epoca = self._epoca
                # El prefetch nunca supera los slots del pool: un mensaje
                # entregado que espera a que se libere un thread es un mensaje
                # que corre contra el `consumer_timeout` del broker sin que
                # nadie lo este trabajando.
                self._channel.basic_qos(prefetch_count=max(1, prefetch),
                                        global_qos=len(colas) > 1)
                self._tags = []
                for cola in colas:
                    self._tags.append(self._channel.basic_consume(
                        queue=cola,
                        on_message_callback=self._callback(on_message, epoca)))
                self._log.info("consumiendo %s (prefetch=%s)", ", ".join(colas),
                               prefetch)
                espera = 1
                self._channel.start_consuming()
            except Exception as exc:                            # pragma: no cover
                if self._stop.is_set():
                    break
                self._log.warning("se corto el consumo (%s); reintento en %ss",
                                  exc, espera)
                self._conn = None
                self._channel = None
                time.sleep(espera)
                espera = min(espera * 2, 30)
            finally:
                self._consuming = False

    def _callback(self, on_message, epoca: int):
        def _recibido(canal, method, properties, body):
            entrega = Delivery(body=body,
                               receipt=f"{epoca}:{method.delivery_tag}",
                               queue=method.routing_key or "")
            try:
                on_message(entrega)
            except Exception as exc:                            # pragma: no cover
                # Que el despacho falle no puede romper el loop de IO: el
                # mensaje vuelve a la cola y lo toma otro.
                self._log.exception("fallo despachando el mensaje: %s", exc)
                self.nack(entrega.receipt, requeue=True)

        return _recibido

    # ---------------------------------------------------------- acuses

    def ack(self, receipt: str) -> None:
        self._acusar(receipt, ack=True, requeue=False)

    def nack(self, receipt: str, *, requeue: bool) -> None:
        self._acusar(receipt, ack=False, requeue=requeue)

    def _acusar(self, receipt: str, *, ack: bool, requeue: bool) -> None:
        epoca, tag = receipt.split(":", 1)
        if int(epoca) != self._epoca:
            # Hubo reconexion: el broker ya redeliverio este mensaje y el tag
            # apunta a otro. Confirmarlo seria ackear trabajo ajeno.
            self._log.warning("acuse de una epoca vieja (%s != %s), se descarta",
                              epoca, self._epoca)
            return
        canal, conexion, esperada = self._channel, self._conn, self._epoca

        def _hacerlo():
            if self._epoca != esperada:
                return
            try:
                if ack:
                    canal.basic_ack(delivery_tag=int(tag))
                else:
                    canal.basic_nack(delivery_tag=int(tag), requeue=requeue)
            except Exception as exc:                            # pragma: no cover
                self._log.error("no se pudo %s el tag %s: %s",
                                "ackear" if ack else "nackear", tag, exc)

        if conexion is None or not conexion.is_open:            # pragma: no cover
            self._log.warning("sin conexion para el acuse; el broker redeliverea")
            return
        conexion.add_callback_threadsafe(_hacerlo)

    # ---------------------------------------------------------- cierre

    #: Segundos que vale una medicion de profundidad. Cada cola es un round
    #: trip al broker y `/health` lo puede pedir seguido; un segundo alcanza
    #: para que la lectura sea util y para que un refresh compulsivo no le
    #: agregue trafico a RabbitMQ.
    DEPTH_CACHE_S = 1.0

    #: Cuanto espera la medicion por el canal antes de rendirse.
    #:
    #: Es lo que garantiza que la telemetria NUNCA le haga esperar a un publish.
    #: Medir comparte el canal con quien publica —pika no deja usarlo desde dos
    #: threads— y si el broker se pone lento, el que tiene que ceder es el que
    #: solo estaba mirando: un import encolandose no puede quedar atras de tres
    #: `queue_declare` de un /health. Si no consigue el turno, se devuelve la
    #: ultima medicion conocida y `age_s` en /health delata que quedo vieja.
    DEPTH_LOCK_WAIT_S = 0.25

    def depths(self) -> dict[str, int]:
        ahora = time.monotonic()
        cacheado = self._depths_cache
        if cacheado is not None and (ahora - cacheado[0]) < self.DEPTH_CACHE_S:
            return cacheado[1]

        if not self._pub_lock.acquire(timeout=self.DEPTH_LOCK_WAIT_S):
            self._log.debug("no consegui el canal para medir las colas; "
                            "devuelvo la ultima medicion")
            return cacheado[1] if cacheado is not None else {}
        try:
            medidas: dict[str, int] = {}
            for cola in (*self.names.all_work, self.names.dlq):
                try:
                    # `passive` no crea nada: pregunta por una cola que ya
                    # declaramos al arrancar. Si el broker no esta, se devuelve
                    # lo que se pudo medir en vez de romper /health.
                    resultado = self._canal_publicacion().queue_declare(
                        queue=cola, passive=True)
                    medidas[cola] = int(resultado.method.message_count)
                except Exception as exc:
                    # Un passive declare que falla CIERRA el canal, asi que hay
                    # que tirarlo: si no, el proximo publish sale por un canal
                    # muerto y recien ahi se entera.
                    self._log.debug("no pude medir la cola %s: %s", cola, exc)
                    self._reset_publicacion()
                    break
        finally:
            self._pub_lock.release()
        self._depths_cache = (ahora, medidas)
        return medidas

    def stop(self) -> None:
        self._stop.set()
        conexion, canal = self._conn, self._channel

        def _cortar():                                          # pragma: no cover
            try:
                if canal is not None and canal.is_open:
                    for tag in self._tags:
                        canal.basic_cancel(tag)
                    canal.stop_consuming()
            except Exception as exc:
                self._log.warning("error cancelando el consumo: %s", exc)

        try:
            if conexion is not None and conexion.is_open:
                conexion.add_callback_threadsafe(_cortar)
        except Exception as exc:                                # pragma: no cover
            self._log.warning("no se pudo señalizar el corte: %s", exc)

    def close(self) -> None:                                    # pragma: no cover
        for recurso in (self._pub_conn, self._conn):
            try:
                if recurso is not None and recurso.is_open:
                    recurso.close()
            except Exception:
                pass
        self._conn = self._channel = self._pub_conn = self._pub_channel = None

