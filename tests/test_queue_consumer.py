"""Que hace el worker cuando las cosas salen mal, que es cuando importa.

Una cola at-least-once entrega de mas: por un canal cortado, por un nodo que
murio, por un deploy. Todo lo que se prueba aca es la politica del consumidor
frente a esa realidad — que se reintenta, que se aparta, que se confirma y que
se devuelve al bajar. El broker es de mentira porque lo que puede estar mal no
es pika, es la decision.
"""
import threading
import time

import pytest

from smart_import.artifacts.local import LocalArtifactStore
from smart_import.config import Config
from smart_import.jobs import FAILED, GEOCODE_FAILED, GEOCODING, JobStore
from smart_import.queue.consumer import RUN_LOCK_TTL_MIN_S, Consumer, demora_para
from smart_import.queue.envelope import (
    GEOCODE, NORMALIZE, InvalidTask, Task, geocode_task, normalize_task,
)
from smart_import.worker.handlers import NormalizeFailed, WorkerContext
from smart_import.worker.tasks import JobDesaparecido
from tests.fake_broker import FakeBroker, PoolSincrono


@pytest.fixture
def escenario_cfg(tmp_path, monkeypatch):
    """Arma el consumidor, con la config que le pida cada test."""
    def armar(**overrides):
        cfg = Config.from_env().replace(role="worker", worker_slots=2,
                                        max_requeue_attempts=3,
                                        work_dir=str(tmp_path), **overrides)
        store = JobStore(tmp_path)
        ctx = WorkerContext(cfg=cfg, store=store,
                            artifacts=LocalArtifactStore(tmp_path),
                            logger=__import__("logging").getLogger("test"))
        hechas: list[Task] = []
        resultado: dict = {"excepcion": None, "antes": None}

        def falso_run_task(_ctx, task):
            hechas.append(task)
            if resultado["antes"] is not None:
                resultado["antes"](task)
            if resultado["excepcion"] is not None:
                raise resultado["excepcion"]

        monkeypatch.setattr("smart_import.queue.consumer.run_task", falso_run_task)
        broker = FakeBroker()
        consumer = Consumer(cfg, broker, lambda: ctx, pool=PoolSincrono())
        return consumer, broker, store, hechas, resultado

    return armar


@pytest.fixture
def escenario(escenario_cfg):
    """Un consumidor con un handler de mentira, para dictar como termina cada tarea."""
    return escenario_cfg()


def _job(store, status=None):
    job = store.create("entregas.csv", "vepathos_flat_v1")
    if status:
        job.touch(status)
        store.save(job)
    return job


# --------------------------------------------------------------- camino feliz

def test_una_tarea_que_sale_bien_se_confirma_una_sola_vez(escenario):
    consumer, broker, store, hechas, _ = escenario
    job = _job(store)

    consumer.on_message(broker.entregar(normalize_task(job.id)))

    assert [t.job_id for t in hechas] == [job.id]
    assert len(broker.ackeadas) == 1
    assert broker.publicadas == [] and broker.dlq == []


def test_el_acuse_es_lo_ultimo(escenario):
    """Si el nodo muere antes de terminar, la tarea tiene que seguir en la cola."""
    consumer, broker, store, _, resultado = escenario
    job = _job(store)
    visto: list[int] = []
    resultado["antes"] = lambda _t: visto.append(len(broker.ackeadas))

    consumer.on_message(broker.entregar(normalize_task(job.id)))

    assert visto == [0], "se ackeo antes de hacer el trabajo"
    assert len(broker.ackeadas) == 1


# --------------------------------------------------------------- reintentos

def test_una_falla_pasajera_vuelve_a_la_cola_con_demora(escenario):
    consumer, broker, store, _, resultado = escenario
    job = _job(store)
    resultado["excepcion"] = ConnectionError("redis no respondio")

    consumer.on_message(broker.entregar(normalize_task(job.id)))

    assert len(broker.publicadas) == 1
    reintento = broker.publicadas[0]
    assert reintento.task.attempt == 2
    assert reintento.delay_s == demora_para(1)
    assert "redis" in reintento.task.error
    assert len(broker.ackeadas) == 1, "el original se confirma tras republicar"
    assert broker.nackeadas == [], "nackear con requeue lo devolveria al instante"


def test_al_superar_el_tope_va_a_la_dlq_y_el_job_queda_fallido(escenario):
    """Un job que fracasa tiene que dejar de estar ocupado, o la UI gira para siempre."""
    consumer, broker, store, _, resultado = escenario
    job = _job(store)
    resultado["excepcion"] = ConnectionError("sigue sin haber nadie")

    ultimo = Task(type=NORMALIZE, job_id=job.id, attempt=3)   # tope = 3
    consumer.on_message(broker.entregar(ultimo))

    assert broker.publicadas == [], "no se reintenta mas"
    assert len(broker.dlq) == 1
    tarea, motivo = broker.dlq[0]
    assert tarea.job_id == job.id and "tope" in motivo
    assert store.get(job.id).status == FAILED
    assert store.get(job.id).error == motivo


def test_un_geocode_apartado_no_se_lleva_puesto_el_normalize(escenario):
    """El archivo normalizado sigue siendo descargable: solo fallo la geo."""
    consumer, broker, store, _, resultado = escenario
    job = _job(store, GEOCODING)
    resultado["excepcion"] = ConnectionError("la api no responde")

    consumer.on_message(broker.entregar(
        Task(type=GEOCODE, job_id=job.id, attempt=3), cola="smart-import-geocode"))

    assert store.get(job.id).status == GEOCODE_FAILED


def test_la_demora_crece_pero_tiene_techo():
    assert demora_para(1) < demora_para(2) < demora_para(3)
    assert demora_para(50) == demora_para(60), "sin techo, un reintento tardaria dias"


# --------------------------------------------------------- fallas permanentes

@pytest.mark.parametrize("excepcion", [
    NormalizeFailed("el .xlsx esta corrupto"),
    JobDesaparecido("imp_borrado"),
    InvalidTask("parametros que nadie sabe leer"),
])
def test_lo_que_no_mejora_reintentando_se_confirma_y_se_olvida(escenario, excepcion):
    consumer, broker, store, _, resultado = escenario
    job = _job(store)
    resultado["excepcion"] = excepcion

    consumer.on_message(broker.entregar(normalize_task(job.id)))

    assert len(broker.ackeadas) == 1
    assert broker.publicadas == [], "reintentar un archivo roto ocupa un slot al pedo"
    assert broker.dlq == []


def test_un_mensaje_ilegible_va_a_la_dlq_sin_reencolar(escenario):
    """Va a estar igual de malformado la proxima vez: reencolarlo es un ciclo."""
    consumer, broker, store, hechas, _ = escenario

    consumer.on_message(broker.entregar_crudo(b"{ esto no es una tarea"))

    assert hechas == [], "no se ejecuto nada"
    assert broker.nackeadas == [("1:1", False)]
    assert broker.ackeadas == []


# --------------------------------------------------------------- exclusion

def test_dos_entregas_del_mismo_job_no_corren_a_la_vez(escenario):
    """La segunda se difiere: dos workers escribiendo la misma salida la parten."""
    consumer, broker, store, hechas, _ = escenario
    job = _job(store)
    store.claim_run(job.id, "otro-nodo:1", ttl_s=60)

    consumer.on_message(broker.entregar(normalize_task(job.id)))

    assert hechas == [], "se puso a trabajar sobre un job que tiene otro"
    assert len(broker.publicadas) == 1
    assert broker.publicadas[0].delay_s > 0, "sin demora, el nodo gira en vacio"
    assert len(broker.ackeadas) == 1


def test_diferir_no_gasta_el_presupuesto_de_reintentos(escenario):
    """Un geocode largo entregado dos veces avanza bien: el duplicado espera.

    Si cada espera contara como intento fallido, el duplicado agotaria el tope
    en pocos minutos y marcaria como fallido un job que esta por terminar.
    """
    consumer, broker, store, _, _ = escenario
    job = _job(store)
    store.claim_run(job.id, "otro-nodo:1", ttl_s=60)

    for _ in range(10):                       # el tope es 3
        consumer.on_message(broker.entregar(normalize_task(job.id)))

    assert broker.dlq == [], "un duplicado que espera su turno no es una falla"
    assert all(p.task.attempt == 1 for p in broker.publicadas)


def test_un_mensaje_que_espera_para_siempre_igual_se_aparta(escenario):
    """El techo es el tiempo: mas viejo que su propio trabajo es un huerfano."""
    consumer, broker, store, _, _ = escenario
    job = _job(store)
    store.claim_run(job.id, "otro-nodo:1", ttl_s=60)
    vieja = Task(type=NORMALIZE, job_id=job.id,
                 created_at=time.time() - 10 * 3600)

    consumer.on_message(broker.entregar(vieja))

    assert len(broker.dlq) == 1
    assert store.get(job.id).status != FAILED, (
        "el job lo esta haciendo otro: marcarlo fallido pisa trabajo bueno")


def test_el_lock_se_suelta_al_terminar(escenario):
    consumer, broker, store, _, _ = escenario
    job = _job(store)

    consumer.on_message(broker.entregar(normalize_task(job.id)))

    assert store.claim_run(job.id, "otro-nodo:1", ttl_s=60), "quedo trabado"


def test_el_lock_se_suelta_aunque_la_tarea_falle(escenario):
    consumer, broker, store, _, resultado = escenario
    job = _job(store)
    resultado["excepcion"] = ConnectionError("cualquier cosa")

    consumer.on_message(broker.entregar(normalize_task(job.id)))

    assert store.claim_run(job.id, "otro-nodo:1", ttl_s=60)


def test_el_ttl_del_lock_sale_de_la_config(escenario_cfg):
    """Cuanto se tarda en retomar el trabajo de un nodo muerto es una decision
    de despliegue: en una LAN 15 s alcanzan, con workers en casas ajenas no."""
    consumer, _, store, _, _ = escenario_cfg(run_lock_ttl_s=15)
    job = _job(store)

    consumer.on_message(consumer.broker.entregar(normalize_task(job.id)))

    assert consumer.run_lock_ttl == 15
    assert consumer.demora_ocupado == 5, "no se pregunta mas seguido que el piso"


def test_un_ttl_ridiculo_no_se_aplica(escenario_cfg):
    """Debajo del piso no se retoma antes: se duplica trabajo de nodos VIVOS.

    Un TTL de 2 s significa que cualquier pausa de GC o hipo de red convierte a
    un worker sano en un muerto a los ojos del resto, y el archivo se procesa
    dos veces. El piso lo impide, y el arranque lo dice en vez de callarselo.
    """
    from smart_import.worker.main import revisar_configuracion

    consumer, *_ = escenario_cfg(run_lock_ttl_s=2)

    assert consumer.run_lock_ttl == RUN_LOCK_TTL_MIN_S
    avisos = revisar_configuracion(consumer.cfg.replace(
        role="worker", rabbitmq_host="r", redis_host="r",
        api_url="http://x", worker_token="t"))
    assert any("RUN_LOCK_TTL_S" in a for a in avisos)


def test_el_mismo_nodo_puede_retomar_su_propia_tarea(escenario):
    """Un reintento en el mismo worker no se puede bloquear a si mismo."""
    consumer, broker, store, hechas, _ = escenario
    job = _job(store)
    store.claim_run(job.id, consumer.holder, ttl_s=60)

    consumer.on_message(broker.entregar(normalize_task(job.id)))

    assert len(hechas) == 1


# --------------------------------------------------------------- idempotencia

def test_la_misma_tarea_dos_veces_termina_igual(escenario):
    """Redelivery: el broker no promete entregar una sola vez."""
    consumer, broker, store, hechas, _ = escenario
    job = _job(store)
    task = normalize_task(job.id)

    consumer.on_message(broker.entregar(task))
    consumer.on_message(broker.entregar(task))

    assert len(hechas) == 2, "las dos se ejecutan; el handler es idempotente"
    assert len(broker.ackeadas) == 2
    assert broker.dlq == [] and broker.publicadas == []


# --------------------------------------------------------------- timeouts

def test_una_tarea_colgada_se_aparta_y_el_job_deja_de_estar_ocupado(escenario):
    """Un thread no se puede matar, pero el usuario no puede quedar esperando."""
    consumer, broker, store, _, _ = escenario
    job = _job(store, GEOCODING)
    task = geocode_task(job.id)
    entrega = broker.entregar(task, cola="smart-import-geocode")
    # Se simula el vuelo: la tarea entro hace mas tiempo que su tope.
    from smart_import.queue.consumer import EnVuelo
    consumer._en_vuelo[entrega.receipt] = EnVuelo(
        task=task, receipt=entrega.receipt, vence_en=time.monotonic() - 1)

    assert consumer.revisar_vencidas() == 1

    assert len(broker.dlq) == 1
    assert "supero" in broker.dlq[0][1]
    assert broker.ackeadas == [entrega.receipt]
    assert store.get(job.id).status == GEOCODE_FAILED
    assert not store.get(job.id).busy


def test_una_tarea_a_tiempo_no_se_toca(escenario):
    consumer, broker, store, _, _ = escenario
    job = _job(store)
    task = normalize_task(job.id)
    entrega = broker.entregar(task)
    from smart_import.queue.consumer import EnVuelo
    consumer._en_vuelo[entrega.receipt] = EnVuelo(
        task=task, receipt=entrega.receipt, vence_en=time.monotonic() + 60)

    assert consumer.revisar_vencidas() == 0
    assert broker.dlq == []


# --------------------------------------------------------------- bajada

def test_al_bajar_se_devuelve_lo_que_llega(escenario):
    """Empezar una tarea para abandonarla a la mitad es peor que no tomarla."""
    consumer, broker, store, hechas, _ = escenario
    job = _job(store)
    consumer.stop()

    consumer.on_message(broker.entregar(normalize_task(job.id)))

    assert hechas == []
    assert broker.nackeadas == [("1:1", True)], "tiene que volver a la cola"


def test_al_bajar_se_termina_lo_que_ya_empezo(tmp_path, monkeypatch):
    """Drenaje: lo que esta en vuelo se completa antes de cerrar."""
    cfg = Config.from_env().replace(role="worker", worker_slots=2,
                                    shutdown_drain_s=5, work_dir=str(tmp_path))
    store = JobStore(tmp_path)
    ctx = WorkerContext(cfg=cfg, store=store,
                        artifacts=LocalArtifactStore(tmp_path),
                        logger=__import__("logging").getLogger("test"))
    empezo = threading.Event()
    termino = threading.Event()

    def tarea_lenta(_ctx, _task):
        empezo.set()
        time.sleep(0.3)
        termino.set()

    monkeypatch.setattr("smart_import.queue.consumer.run_task", tarea_lenta)
    broker = FakeBroker()
    consumer = Consumer(cfg, broker, lambda: ctx)          # pool real, con threads
    job = _job(store)

    consumer.on_message(broker.entregar(normalize_task(job.id)))
    assert empezo.wait(2), "la tarea no arranco"
    consumer.stop()
    consumer._drenar()

    assert termino.is_set(), "se corto una tarea en vuelo"
    assert len(broker.ackeadas) == 1
    assert broker.cerrado


def test_dos_señales_seguidas_no_rompen_la_bajada(escenario):
    consumer, broker, _, _, _ = escenario
    consumer.stop()
    consumer.stop()
    consumer._drenar()


# --------------------------------------------------------------- suscripcion

def test_un_nodo_sin_geocode_no_se_suscribe_a_esa_cola(tmp_path):
    """Si se suscribiera, tomaria tareas que solo puede fallar."""
    cfg = Config.from_env().replace(role="worker", consume_normalize=True,
                                    consume_geocode=False, work_dir=str(tmp_path),
                                    queue_prefix="smart-import")
    consumer = Consumer(cfg, FakeBroker(), lambda: None, pool=PoolSincrono())

    assert consumer.colas == ["smart-import-normalize"]

    con_geo = Consumer(cfg.replace(consume_geocode=True), FakeBroker(),
                       lambda: None, pool=PoolSincrono())
    assert con_geo.colas == ["smart-import-normalize", "smart-import-geocode"]


def test_el_prefetch_no_supera_los_slots(tmp_path):
    """Un mensaje entregado sin thread libre corre contra el consumer_timeout."""
    cfg = Config.from_env().replace(role="worker", worker_slots=3,
                                    work_dir=str(tmp_path))
    broker = FakeBroker()
    consumer = Consumer(cfg, broker, lambda: None, pool=PoolSincrono())

    consumer.broker.consume(consumer.colas, consumer.slots, consumer.on_message)

    assert broker.prefetch == 3


# ---------------------------------------------- slots colgados, no historicos

def test_una_tarea_lenta_que_igual_termina_devuelve_su_slot(escenario_cfg):
    """El nodo no se puede apagar por tareas viejas que terminaron bien.

    El vigia aparta lo que se paso del tope, pero un thread de Python no se
    puede matar: puede seguir y terminar un segundo despues. Cuando eso pasa el
    slot vuelve a estar libre. Contarlo para siempre hacia que un worker de dos
    slots se apagara solo despues de dos geocodes lentos repartidos en horas
    —por ejemplo dos zonas nuevas que tuvieron que construir su indice— aunque
    en ese momento no tuviera nada corriendo.
    """
    consumer, broker, store, _, _ = escenario_cfg(task_timeout_normalize_s=0)
    job = store.create("x.csv", "vepathos")

    for _ in range(consumer.slots + 2):
        entrega = broker.entregar(normalize_task(job.id))
        consumer.on_message(entrega)             # PoolSincrono: corre y termina
        assert consumer.revisar_vencidas() == 0, "no deberia quedar nada en vuelo"

    assert consumer._colgadas == set()
    assert not consumer._drenando.is_set(), "se apago por tareas que terminaron"


def test_con_todos_los_slots_realmente_colgados_se_corta_el_consumo(escenario_cfg):
    """Un nodo sin slots utiles miente si sigue tomando trabajo: mejor bajar."""
    consumer, broker, store, _, _ = escenario_cfg(task_timeout_normalize_s=0)
    job = store.create("x.csv", "vepathos")

    # Tareas que quedan EN VUELO: se registran y nunca se resuelven.
    for i in range(consumer.slots):
        entrega = broker.entregar(normalize_task(job.id))
        vuelo = __import__("smart_import.queue.consumer", fromlist=["EnVuelo"]).EnVuelo(
            task=Task.from_bytes(entrega.body), receipt=entrega.receipt,
            vence_en=time.monotonic() - 1)
        with consumer._candado:
            consumer._en_vuelo[entrega.receipt] = vuelo

    assert consumer.revisar_vencidas() == consumer.slots
    assert len(consumer._colgadas) == consumer.slots
    assert consumer._drenando.is_set(), "no corto el consumo con todo colgado"


# ------------------------------------------- el job se marca antes del acuse

def test_si_no_se_puede_marcar_el_job_no_se_ackea(escenario, monkeypatch):
    """Sin la marca, el job queda ocupado para siempre y nadie lo destraba.

    El mensaje ya esta en la DLQ, que no tiene consumidor: si ademas se ackea,
    no queda nada que pueda volver a intentar marcarlo. Fallar antes del acuse
    deja que el broker redeliverea y se reintente el apartado entero.
    """
    consumer, broker, store, _, resultado = escenario
    job = store.create("x.csv", "vepathos")
    resultado["excepcion"] = RuntimeError("se cayo algo")

    def store_roto(*_a, **_k):
        raise ConnectionError("Redis no responde")

    monkeypatch.setattr(store, "get", store_roto)

    tarea = Task(type=NORMALIZE, job_id=job.id,
                 attempt=int(consumer.cfg.max_requeue_attempts))
    consumer.on_message(broker.entregar(tarea))

    assert broker.ackeadas == [], "ackeo sin haber podido marcar el job"


def test_el_job_se_marca_fallido_antes_de_ackear(escenario):
    """El orden correcto: primero la marca, despues la DLQ, despues el acuse."""
    consumer, broker, store, _, resultado = escenario
    job = store.create("x.csv", "vepathos")
    resultado["excepcion"] = RuntimeError("se cayo algo")

    tarea = Task(type=NORMALIZE, job_id=job.id,
                 attempt=int(consumer.cfg.max_requeue_attempts))
    consumer.on_message(broker.entregar(tarea))

    assert store.get(job.id).status == FAILED
    assert len(broker.dlq) == 1
    assert broker.ackeadas, "no ackeo despues de dejar todo consistente"
