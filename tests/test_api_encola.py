"""La API con el trabajo repartido: encola, espera un rato y no miente.

Lo que se prueba es el contrato visto desde el cliente, que es lo unico que no
puede cambiar: si el resultado llega a tiempo, la respuesta tiene que ser la
MISMA que da el modo de un solo proceso (201 con el job entero); si no llega,
un 202 con el job_id y por donde seguirlo. Nada de esto necesita RabbitMQ: el
broker de mentira ejecuta la tarea con los handlers de verdad, que es lo que
haria un worker.
"""
import importlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from smart_import.jobs import ANALYZING, FAILED, JobStore
from smart_import.queue.envelope import GEOCODE, NORMALIZE
from smart_import.worker.handlers import WorkerContext
from smart_import.worker.tasks import run_task
from tests.conftest import FIXTURES
from tests.fake_broker import FakeBroker

api_module = importlib.import_module("smart_import.api.app")


class BrokerConWorker(FakeBroker):
    """Un broker con un worker atado: la tarea se hace cuando se publica.

    `demora` simula un nodo lento sin dormir el test: el trabajo arranca en otro
    thread y el endpoint tiene que decidir mientras tanto.
    """

    def __init__(self, ctx, demora: float = 0.0, ejecutar: bool = True):
        super().__init__()
        self._ctx = ctx
        self._demora = demora
        self._ejecutar = ejecutar
        self.threads: list[threading.Thread] = []

    def publish(self, task, *, delay_s: float = 0.0) -> None:
        super().publish(task, delay_s=delay_s)
        if not self._ejecutar:
            return

        def trabajar():
            time.sleep(self._demora)
            run_task(self._ctx, task)

        hilo = threading.Thread(target=trabajar, daemon=True)
        self.threads.append(hilo)
        hilo.start()

    def esperar_workers(self, timeout: float = 20.0) -> None:
        for hilo in self.threads:
            hilo.join(timeout)


@pytest.fixture
def api(tmp_path, monkeypatch):
    """La API en rol `api`: recibe archivos y encola, no procesa nada."""
    cfg = api_module.CFG.replace(role="api", work_dir=str(tmp_path),
                                 default_wait_s=10.0, geocoding_enabled=True,
                                 pbf_dir="/tmp/pbf")
    artefactos = api_module.make_artifact_store(cfg)
    store = JobStore(tmp_path)
    monkeypatch.setattr(api_module, "CFG", cfg)
    monkeypatch.setattr(api_module, "store", store)
    monkeypatch.setattr(api_module, "artifacts", artefactos)
    ctx = WorkerContext(cfg=cfg, store=store, artifacts=artefactos,
                        logger=api_module.logger)
    return cfg, store, ctx


def _con_broker(api, **kwargs) -> tuple[TestClient, BrokerConWorker]:
    _, _, ctx = api
    broker = BrokerConWorker(ctx, **kwargs)
    api_module.broker = broker
    return TestClient(api_module.app), broker


@pytest.fixture(autouse=True)
def _sin_broker_al_salir():
    yield
    api_module.broker = None


def _subir(client, nombre="es_sin_coords.csv", **params):
    with open(FIXTURES / nombre, "rb") as fh:
        return client.post("/imports", files={"file": (nombre, fh.read())},
                           params=params)


# --------------------------------------------------------------- normalize

def test_si_el_worker_llega_a_tiempo_la_respuesta_es_la_de_siempre(api):
    """201 con el job entero: para el cliente, que trabaje otro nodo es invisible."""
    client, broker = _con_broker(api)

    r = _subir(client)

    assert r.status_code == 201
    cuerpo = r.json()
    assert cuerpo["busy"] is False
    assert cuerpo["report"]["rows_output"] > 0
    assert "download" in {a["action"] for a in cuerpo["next_actions"]}
    assert [p.task.type for p in broker.publicadas] == [NORMALIZE]


def test_el_trabajo_pesado_no_lo_hace_la_api(api):
    """Sin nadie consumiendo, el archivo queda encolado y el job ocupado."""
    client, broker = _con_broker(api, ejecutar=False)

    r = _subir(client, wait=0)

    assert r.status_code == 202
    cuerpo = r.json()
    assert cuerpo["busy"] is True
    assert cuerpo["status"] == ANALYZING
    assert cuerpo["poll"] == f"/imports/{cuerpo['job_id']}"
    assert cuerpo["events"].endswith("/events")
    assert len(broker.publicadas) == 1


def test_si_tarda_mas_que_la_espera_se_responde_202_y_el_job_sigue(api):
    """El 202 no es un error: el trabajo sigue y el cliente lo mira por poll/SSE."""
    _, store, _ = api
    client, broker = _con_broker(api, demora=0.6)

    r = _subir(client, wait=0.1)

    assert r.status_code == 202
    job_id = r.json()["job_id"]
    broker.esperar_workers()
    assert store.get(job_id).status != ANALYZING, "el worker igual lo termino"
    assert client.get(f"/imports/{job_id}").json()["busy"] is False


def test_el_archivo_original_queda_en_la_api(api):
    """El worker no tiene disco compartido: lo va a buscar por HTTP."""
    _, store, _ = api
    client, _ = _con_broker(api, ejecutar=False)

    job_id = _subir(client, wait=0).json()["job_id"]

    job = store.get(job_id)
    assert job.raw_path and api_module.artifacts.exists(job_id, "raw", job.raw_path)


def test_los_parametros_del_pedido_viajan_en_la_tarea(api):
    """Si no viajan, el worker normaliza con otros criterios que los que pidieron."""
    client, broker = _con_broker(api, ejecutar=False)

    _subir(client, wait=0, phone_region="AR", diagnostics=True,
           depot_city="Tandil", service_date="2026-03-15")

    params = broker.publicadas[0].task.params
    assert params["phone_region"] == "AR"
    assert params["diagnostics"] is True
    assert params["depot_city"] == "Tandil"
    assert params["service_date"] == "2026-03-15", "la fecha tiene que ser JSON"


def test_un_archivo_que_el_worker_rechaza_sale_422(api):
    """Mismo codigo que en un solo proceso: el cliente no distingue quien fallo."""
    _, store, _ = api
    client, broker = _con_broker(api, ejecutar=False)

    def worker_que_rechaza(task, **kw):
        # Lo que hace `run_normalize_job` con un archivo que no puede leer.
        job = store.get(task.job_id)
        job.error = "el archivo esta corrupto"
        job.touch(FAILED)
        store.save(job)

    broker.publish = worker_que_rechaza
    r = _subir(client)

    assert r.status_code == 422
    assert "corrupto" in r.json()["detail"]
    assert store.get(store.list()[0].id).status == FAILED, "el job queda para mirarlo"


def test_si_la_cola_no_esta_se_dice_al_toque(api):
    """Aceptar el archivo sin poder encolarlo deja un job que nadie va a terminar."""
    _, store, _ = api
    client, broker = _con_broker(api, ejecutar=False)

    def broker_caido(task, **kw):
        raise RuntimeError("no se pudo publicar en acc-normalize: conexion rechazada")

    broker.publish = broker_caido
    r = _subir(client)

    assert r.status_code == 503
    assert "reintent" in r.json()["detail"].lower()
    guardado = store.list()[0]
    assert guardado.busy is False, "un job ocupado que nadie tomo gira para siempre"
    assert guardado.status == FAILED


def test_el_estado_ocupado_se_escribe_antes_de_publicar(api):
    """Al reves, un worker rapido termina y el `analyzing` tardio le pisa el
    resultado: el job queda en proceso para siempre."""
    _, store, _ = api
    client, broker = _con_broker(api, ejecutar=False)
    vistos: list[str] = []

    original = broker.publish

    def espiar(task, **kw):
        vistos.append(store.get(task.job_id).status)
        return original(task, **kw)

    broker.publish = espiar
    _subir(client, wait=0)

    assert vistos == [ANALYZING]


def test_el_handler_no_anuncia_el_final_antes_de_empezar(api):
    """`busy` es lo UNICO que mira la API para saber si ya hay resultado.

    Cuando el normalize corria inline nadie podia ver los estados intermedios,
    asi que el handler podia marcar `normalized` al arrancar sin consecuencias.
    Con el trabajo en otro nodo, ese anuncio prematuro hace que `POST /imports`
    devuelva 201 con el report vacio: el job dice que termino y todavia no
    escribio nada.
    """
    from smart_import.worker.handlers import run_normalize_job

    cfg, store, ctx = api
    client, broker = _con_broker(api, ejecutar=False)
    job_id = _subir(client, wait=0).json()["job_id"]
    estados: list[tuple[str, bool]] = []
    guardar = store.save
    store.save = lambda job: (estados.append((job.status, job.busy)), guardar(job))[1]

    run_normalize_job(ctx, store.get(job_id),
                      api_module._schema_path("vepathos_flat_v1"), None, False)

    assert estados, "el handler no guardo nada"
    assert all(ocupado for _, ocupado in estados[:-1]), (
        f"anuncio el final antes de tiempo: {estados}")
    assert estados[-1][1] is False


# --------------------------------------------------------------- mapping

def test_confirmar_mapping_tambien_encola(api):
    client, broker = _con_broker(api)
    job_id = _subir(client).json()["job_id"]

    r = client.put(f"/imports/{job_id}/mapping",
                   json={"Direccion": "address"}, params={"wait": 10})

    assert r.status_code == 200
    assert [p.task.type for p in broker.publicadas] == [NORMALIZE, NORMALIZE]
    assert broker.publicadas[1].task.params["manual_mapping"] == {"Direccion": "address"}


# --------------------------------------------------------------- geocode

def test_el_geocode_se_encola_en_su_propia_cola(api, monkeypatch):
    """Es otra cola porque no todo nodo puede hacerlo: hace falta el PBF."""
    _, _, ctx = api
    client, broker = _con_broker(api, ejecutar=False)
    job_id = _subir(client, wait=0).json()["job_id"]
    run_task(ctx, broker.publicadas[0].task)          # el normalize, a mano
    # El pool de la API no puede recibir nada: con roles, el trabajo es del worker.
    monkeypatch.setattr(api_module._geocode_pool, "submit",
                        lambda *a, **k: pytest.fail("la api ejecuto el geocode"))

    r = client.post(f"/imports/{job_id}/geocode", params={"depot_city": "Tandil"})

    assert r.status_code == 202
    encolada = broker.publicadas[-1].task
    assert encolada.type == GEOCODE
    assert encolada.params["depot_city"] == "Tandil"
    assert r.json()["status"] == "geocode_queued"


def test_dos_geocodes_seguidos_encolan_uno_solo(api):
    """La reserva sigue siendo de la API: encolar dos veces duplicaria el trabajo."""
    _, store, ctx = api
    client, broker = _con_broker(api, ejecutar=False)
    # El normalize se hace a mano: lo que se mide es la puerta del geocode, y un
    # worker de verdad podria terminar (o fallar) el geocode entre los dos POST.
    job_id = _subir(client, wait=0).json()["job_id"]
    run_task(ctx, broker.publicadas[0].task)

    primero = client.post(f"/imports/{job_id}/geocode", params={"depot_city": "Tandil"})
    segundo = client.post(f"/imports/{job_id}/geocode", params={"depot_city": "Tandil"})

    assert primero.status_code == 202 and segundo.status_code == 409
    assert [p.task.type for p in broker.publicadas].count(GEOCODE) == 1


# ------------------------------------- si la cola no responde, nada queda trabado

class BrokerCaido(FakeBroker):
    """El broker se cayo justo entre reservar el job y publicar la tarea."""

    def publish(self, task, *, delay_s: float = 0.0) -> None:
        raise ConnectionError("RabbitMQ no responde")


def test_si_no_se_puede_encolar_el_normalize_el_job_no_queda_ocupado(api):
    _, store, _ = api
    api_module.broker = BrokerCaido()
    client = TestClient(api_module.app)

    res = _subir(client)

    assert res.status_code == 503
    jobs = store.list(10)
    assert jobs, "no se creo el job"
    assert not jobs[0].busy, "quedo ocupado con la tarea nunca encolada"


def test_si_no_se_puede_encolar_el_geocode_se_puede_reintentar(api):
    """El caso que dejaba un job trabado un dia entero.

    `claim_geocode` ya habia reservado y guardado el job como ocupado cuando el
    publish fallaba. Sin revertir eso: `next_actions` vacio, spinner eterno, el
    barrido no lo toca por estar ocupado, y el reintento del usuario rebotando
    contra un 409 «ya hay una geolocalizacion en curso» que no era cierto.
    """
    cfg, store, ctx = api
    client, _ = _con_broker(api)
    res = _subir(client)
    job_id = res.json()["job_id"]
    assert store.get(job_id).needs_geocode > 0

    api_module.broker = BrokerCaido()
    falla = client.post(f"/imports/{job_id}/geocode",
                        params={"depot_city": "Buenos Aires", "depot_country": "AR"})
    assert falla.status_code == 503

    job = store.get(job_id)
    assert not job.busy, "quedo ocupado sin tarea en la cola"
    assert job.status == "geocode_failed", job.status
    # Y sobre todo: el normalize sigue sirviendo y se puede volver a intentar.
    assert job.normalized_path
    assert "geocode" in {a["action"] for a in job.next_actions()}
