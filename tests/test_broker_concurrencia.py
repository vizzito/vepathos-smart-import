"""El canal de pika lo tocan dos threads, y eso hay que serializarlo.

`BlockingConnection` no es thread-safe, y en el rol api hay dos escritores sobre
el MISMO canal: los `basic_publish` de los endpoints y el `queue_declare(passive)`
con el que la flota mide las colas cada diez segundos. Sin lock, sus frames AMQP
se entrelazan y el canal se cae cada tanto sin patron aparente.

Pero el lock trae su propio riesgo, y es el que estos tests fijan: medir NUNCA
puede hacer esperar a quien publica. Un import encolandose no puede quedar atras
de tres `queue_declare` de un /health contra un broker lento.
"""
import threading
import time

import pytest

from smart_import.config import Config
from smart_import.queue.broker import RabbitBroker
from smart_import.queue.envelope import normalize_task


@pytest.fixture
def broker():
    cfg = Config.from_env().replace(queue_prefix="smart-import")
    return RabbitBroker(cfg)


class CanalFalso:
    """Un canal que registra si dos threads lo usan a la vez."""

    def __init__(self, demora: float = 0.0):
        self.demora = demora
        self.adentro = 0
        self.solapamientos = 0
        self.publicados: list[str] = []
        self._candado = threading.Lock()

    def _entrar(self):
        with self._candado:
            self.adentro += 1
            if self.adentro > 1:
                self.solapamientos += 1

    def _salir(self):
        with self._candado:
            self.adentro -= 1

    def basic_publish(self, exchange, routing_key, body, properties=None):
        self._entrar()
        time.sleep(self.demora)
        self.publicados.append(routing_key)
        self._salir()

    def queue_declare(self, queue, passive=False, **kw):
        self._entrar()
        time.sleep(self.demora)
        self._salir()

        class _M:
            method = type("m", (), {"message_count": 7})()
        return _M()


def _cablear(broker, canal):
    """Le pone un canal de mentira, saltando la conexion real."""
    broker._conn = type("c", (), {"is_open": True})()
    broker._channel = canal
    broker._conectar = lambda: None
    return broker


def test_publicar_y_medir_no_se_pisan(broker):
    """La prueba directa del bug: dos threads sobre el mismo canal."""
    canal = CanalFalso(demora=0.002)
    _cablear(broker, canal)

    fin = threading.Event()

    def publicar():
        while not fin.is_set():
            broker.publish(normalize_task("imp_1"))

    def medir():
        while not fin.is_set():
            broker._depths_cache = None      # forzar medicion real cada vez
            broker.depths()

    hilos = [threading.Thread(target=publicar), threading.Thread(target=medir)]
    for h in hilos:
        h.start()
    time.sleep(0.4)
    fin.set()
    for h in hilos:
        h.join(5)

    assert canal.solapamientos == 0, (
        f"{canal.solapamientos} accesos simultaneos al canal de pika")
    assert canal.publicados, "el test no llego a publicar nada"


def test_medir_le_cede_el_turno_a_quien_publica(broker):
    """La telemetria no puede bloquear al trabajo.

    Con el lock tomado por otro, `depths()` se rinde en vez de esperar: devuelve
    la ultima medicion conocida y `age_s` en /health delata que quedo vieja.
    """
    canal = CanalFalso()
    _cablear(broker, canal)
    broker._depths_cache = (0.0, {"smart-import-normalize": 99})

    tomado = threading.Event()
    soltar = threading.Event()

    def ocupar():
        with broker._pub_lock:
            tomado.set()
            soltar.wait(5)

    hilo = threading.Thread(target=ocupar)
    hilo.start()
    tomado.wait(5)
    try:
        arranque = time.monotonic()
        medido = broker.depths()
        tardanza = time.monotonic() - arranque
    finally:
        soltar.set()
        hilo.join(5)

    assert tardanza < broker.DEPTH_LOCK_WAIT_S * 4
    assert medido == {"smart-import-normalize": 99}, "no devolvio el cache"


def test_sin_medicion_previa_devuelve_vacio_en_vez_de_esperar(broker):
    """Arrancando no hay cache: mejor `{}` que hacer esperar a un publish."""
    _cablear(broker, CanalFalso())
    tomado, soltar = threading.Event(), threading.Event()

    def ocupar():
        with broker._pub_lock:
            tomado.set()
            soltar.wait(5)

    hilo = threading.Thread(target=ocupar)
    hilo.start()
    tomado.wait(5)
    try:
        assert broker.depths() == {}
    finally:
        soltar.set()
        hilo.join(5)


# ------------------------------------------------------- nombres de cola

def test_las_colas_llevan_el_prefijo_del_despliegue():
    """No se mezclan con las del optimizer, que comparte el mismo RabbitMQ."""
    from smart_import.queue.broker import QueueNames

    nombres = QueueNames("smart-import")
    todas = [*nombres.all_work, nombres.dlq,
             nombres.delay("normalize"), nombres.delay("geocode")]
    assert all(n.startswith("smart-import-") for n in todas), todas
    assert "route-optimizer-tasks" not in todas


def test_el_prefijo_es_configurable_y_aisla_dos_despliegues():
    """Dos stacks contra el mismo broker no se roban tareas entre si."""
    from smart_import.queue.broker import QueueNames

    prod = set(QueueNames("smart-import").all_work)
    staging = set(QueueNames("smart-import-staging").all_work)
    assert prod.isdisjoint(staging), (prod, staging)


# ------------------------------------------------------- heartbeat AMQP

def test_el_heartbeat_sale_de_la_config_y_no_de_una_constante():
    """Un worker remoto habla por NAT o por un tunel SSH.

    Esas rutas cortan las conexiones ociosas entre 5 y 15 minutos. Con el
    default de pika (600) el trafico va cada 300 s, justo en ese borde, y cuando
    la entrada de NAT se borra el consumidor queda "conectado" sobre un socket
    muerto: la cola crece y no hay un error en el log. El sintoma real fue un
    worker reconectando cada pocos minutos con "Transport indicated EOF".
    """
    cfg = Config.from_env().replace(rabbitmq_heartbeat_s=45)
    assert RabbitBroker(cfg)._params().heartbeat == 45


def test_el_default_es_corto_a_proposito():
    """30 s: trafico cada 15, deteccion de caida en ~60. Mismo valor que el
    optimizer, que aprendio esto antes contra este mismo RabbitMQ."""
    assert Config.from_env().rabbitmq_heartbeat_s == 30
