"""Paridad entre los dos backends de estado, y lo que solo el remoto puede romper.

La mayoria de los asserts corren PARAMETRIZADOS contra los dos stores: si el de
Redis se comporta distinto del de memoria en algo que la API da por sentado, el
mismo test falla en una de las dos ejecuciones y se ve exactamente cual. Los
tests de abajo son los que no tienen equivalente en memoria: el estado que
sobrevive al proceso, la reserva entre procesos y el limite de escrituras.
"""
import json
import threading
import time

import pytest

from smart_import.artifacts import LocalArtifactStore
from smart_import.job_store_redis import RedisJobStore
from smart_import.jobs import (
    ANALYZING, COMPLETED, GEOCODE_QUEUED, GEOCODING, NORMALIZED, JobStore,
)
from tests.fake_redis import FakeRedis


@pytest.fixture
def redis_falso():
    return FakeRedis()


@pytest.fixture
def store_redis(redis_falso, tmp_path):
    return RedisJobStore(redis_falso, prefix="test:", ttl_s=3600,
                         artifacts=LocalArtifactStore(tmp_path / "artefactos"))


@pytest.fixture
def store_memoria(tmp_path):
    return JobStore(tmp_path / "jobs")


@pytest.fixture(params=["memoria", "redis"])
def store(request, store_memoria, store_redis):
    """El mismo test, contra los dos backends."""
    return store_memoria if request.param == "memoria" else store_redis


# --------------------------------------------------------------- paridad


def test_crear_y_leer(store):
    job = store.create("entregas raras!.xlsx", "vepathos_flat_v1")

    assert job.id.startswith("imp_")
    assert job.filename == "entregas_raras_.xlsx", "el nombre no se saneo"

    leido = store.get(job.id)
    assert leido is not None
    assert (leido.id, leido.filename, leido.status) == (job.id, job.filename, job.status)


def test_un_job_que_no_existe_es_none(store):
    assert store.get("imp_no_existe") is None


def test_save_persiste_el_estado(store):
    job = store.create("x.csv", "vepathos_flat_v1")
    job.report = {"rows_output": 42, "needs_geocode": 7}
    job.normalized_path = "/data/jobs/x/normalized/normalized.csv"
    job.touch(NORMALIZED)

    store.save(job)

    leido = store.get(job.id)
    assert leido.status == NORMALIZED
    assert leido.report["rows_output"] == 42
    assert leido.needs_geocode == 7
    assert leido.normalized_path == job.normalized_path


def test_un_job_borrado_no_revive_con_una_escritura_tardia(store):
    """El worker termina despues de que el barrido se llevo el job."""
    job = store.create("x.csv", "vepathos_flat_v1")
    assert store.delete(job.id) is True

    job.touch(COMPLETED)
    store.save(job)

    assert store.get(job.id) is None


def test_delete_devuelve_si_habia_algo(store):
    job = store.create("x.csv", "vepathos_flat_v1")
    assert store.delete(job.id) is True
    assert store.delete(job.id) is False


def test_list_devuelve_los_mas_nuevos_primero(store):
    creados = []
    for i in range(3):
        job = store.create(f"{i}.csv", "vepathos_flat_v1")
        job.created_at += i          # el reloj no alcanza para desempatar
        store.save(job)
        creados.append(job.id)

    assert [j.id for j in store.list(limit=50)] == list(reversed(creados))
    assert len(store.list(limit=2)) == 2


def test_len_cuenta_los_jobs(store):
    assert len(store) == 0
    store.create("a.csv", "vepathos_flat_v1")
    store.create("b.csv", "vepathos_flat_v1")
    assert len(store) == 2


def test_solo_uno_se_lleva_la_reserva_del_geocode(store):
    """Doble click, retry del front o reintento de httpx: uno solo arranca."""
    job = store.create("x.csv", "vepathos_flat_v1")
    job.touch(NORMALIZED)
    store.save(job)

    ganados = []
    cerrojo = threading.Lock()
    arrancar = threading.Barrier(8)

    def intentar():
        arrancar.wait()
        ok = store.claim_geocode(job.id)
        with cerrojo:
            ganados.append(ok)

    hilos = [threading.Thread(target=intentar) for _ in range(8)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert sum(ganados) == 1, f"{sum(ganados)} requests lanzaron un worker"
    assert store.get(job.id).status == GEOCODE_QUEUED


def test_no_se_puede_reservar_un_job_inexistente(store):
    assert store.claim_geocode("imp_no_existe") is False


def test_un_job_que_termino_se_puede_volver_a_reservar(store):
    """Reintentar un geocode que fallo es una accion que la UI ofrece."""
    job = store.create("x.csv", "vepathos_flat_v1")
    job.touch(NORMALIZED)
    store.save(job)

    assert store.claim_geocode(job.id) is True

    terminado = store.get(job.id)
    terminado.touch(COMPLETED)
    store.save(terminado)

    assert store.claim_geocode(job.id) is True


def test_el_barrido_no_toca_un_job_ocupado(store):
    """Puede ser un geocode largo construyendo un indice."""
    viejo = store.create("viejo.csv", "vepathos_flat_v1")
    ocupado = store.create("ocupado.csv", "vepathos_flat_v1")
    reciente = store.create("reciente.csv", "vepathos_flat_v1")

    viejo.touch(COMPLETED)
    viejo.updated_at = time.time() - 48 * 3600
    store.save(viejo)
    ocupado.touch(GEOCODING)
    ocupado.updated_at = time.time() - 48 * 3600
    store.save(ocupado)
    reciente.touch(NORMALIZED)
    store.save(reciente)

    assert store.purge_older_than(24 * 3600) == [viejo.id]
    assert store.get(viejo.id) is None
    assert store.get(ocupado.id) is not None, "se borro un geocode en curso"
    assert store.get(reciente.id) is not None


def test_ttl_en_cero_no_barre_nada(store):
    job = store.create("x.csv", "vepathos_flat_v1")
    job.touch(COMPLETED)
    job.updated_at = time.time() - 999 * 3600
    store.save(job)

    assert store.purge_older_than(0) == []
    assert store.get(job.id) is not None


# ------------------------------------------------- solo el store remoto


def test_el_estado_sobrevive_al_proceso(redis_falso, tmp_path):
    """La razon de ser del backend: otro proceso ve el mismo job.

    Dos instancias distintas del store contra el mismo Redis es lo que van a ser
    el rol api y el worker.
    """
    escribe = RedisJobStore(redis_falso, prefix="test:")
    lee = RedisJobStore(redis_falso, prefix="test:")

    job = escribe.create("entregas.csv", "vepathos_flat_v1")
    job.report = {"rows_output": 3}
    job.capabilities = {"geocoding": False}
    job.touch(NORMALIZED)
    escribe.save(job)

    visto = lee.get(job.id)
    assert visto.status == NORMALIZED
    assert visto.report == {"rows_output": 3}
    assert visto.capabilities == {"geocoding": False}, (
        "se perdieron las capacidades: la UI vuelve a ofrecer geocodificar")


def test_get_devuelve_una_copia(store_redis):
    """La propiedad que rompe el aliasing, y por la que existe `save`."""
    job = store_redis.create("x.csv", "vepathos_flat_v1")

    suelto = store_redis.get(job.id)
    suelto.touch(COMPLETED)                      # mutar sin guardar

    assert store_redis.get(job.id).status != COMPLETED


def test_el_avance_no_escribe_una_vez_por_fila(store_redis, redis_falso):
    """El geocode llama a `save_progress` por cada direccion.

    Sin limite, 50k filas son 50k escrituras de red que ademas nadie mira: la UI
    pollea cada 500 ms como mucho.
    """
    job = store_redis.create("x.csv", "vepathos_flat_v1")
    clave = f"test:job:{job.id}"
    antes = redis_falso.escrituras[clave]

    for fila in range(500):
        job.geocode_progress = {"phase": "geocoding", "done": fila, "total": 500}
        job.touch()
        store_redis.save_progress(job)

    escrituras = redis_falso.escrituras[clave] - antes
    assert escrituras <= 5, f"{escrituras} escrituras para 500 filas"


def test_un_cambio_de_estado_nunca_se_limita(store_redis):
    """El limite es solo para el avance: si se comiera el estado final, el job
    quedaria `busy` para siempre y la UI polleando sin parar."""
    job = store_redis.create("x.csv", "vepathos_flat_v1")
    job.touch(GEOCODING)
    store_redis.save(job)

    job.geocode_progress = {"phase": "geocoding", "done": 1, "total": 2}
    store_redis.save_progress(job)               # consume el turno del segundo
    job.touch(COMPLETED)
    store_redis.save(job)                        # y aun asi entra

    assert store_redis.get(job.id).status == COMPLETED


def test_la_reserva_se_libera_sola_si_el_worker_muere(store_redis, redis_falso):
    """`kill -9` a mitad del geocode no puede dejar el job trabado para siempre.

    El TTL del lock es la red: pasado ese plazo, otro lo toma. Aca se simula
    venciendo la clave, que es lo que hace Redis solo.
    """
    job = store_redis.create("x.csv", "vepathos_flat_v1")
    job.touch(NORMALIZED)
    store_redis.save(job)
    assert store_redis.claim_geocode(job.id) is True

    # el worker murio con el job en GEOCODING: nadie escribio el estado final
    trabado = store_redis.get(job.id)
    trabado.touch(GEOCODING)
    store_redis.save(trabado)
    assert store_redis.claim_geocode(job.id) is False

    redis_falso.vencer(f"test:lock:geocode:{job.id}")
    liberado = store_redis.get(job.id)
    liberado.touch(NORMALIZED)                   # lo que haria el reintento
    store_redis.save(liberado)

    assert store_redis.claim_geocode(job.id) is True


def test_un_estado_ilegible_es_un_job_perdido_no_un_500(store_redis, redis_falso):
    job = store_redis.create("x.csv", "vepathos_flat_v1")
    redis_falso.set(f"test:job:{job.id}", "{esto no es json")

    assert store_redis.get(job.id) is None


def test_el_barrido_limpia_lo_que_vencio_el_ttl(store_redis, redis_falso, tmp_path):
    """Si la metadata se vence antes que el barrido, los archivos quedan sin
    nadie que los nombre: el indice es la unica forma de encontrarlos."""
    job = store_redis.create("x.csv", "vepathos_flat_v1")
    artefacto = store_redis._artifacts.reserve(job.id, "flat")
    artefacto.write_text("delivery_id\n", encoding="utf-8")

    redis_falso.vencer(f"test:job:{job.id}")
    store_redis.purge_older_than(3600)

    assert len(store_redis) == 0, "quedo en el indice un job que ya no existe"
    assert not artefacto.exists(), "quedaron archivos huerfanos en disco"


def test_lo_guardado_es_json_plano(store_redis, redis_falso):
    """Se mira desde `redis-cli` cuando algo se traba en produccion."""
    job = store_redis.create("x.csv", "vepathos_flat_v1")

    estado = json.loads(redis_falso.get(f"test:job:{job.id}"))

    assert estado["id"] == job.id
    assert estado["_version"] == 1


# ------------------------------------------------ retencion segun el estado

def test_un_job_en_vuelo_no_se_evapora_con_retencion_corta():
    """Retencion corta es para los RESULTADOS, no para lo que falta procesar.

    Un job encolado no controla cuando lo toman: depende de cuanta cola haya
    adelante y de cuantos workers esten prendidos. Si su estado vence mientras
    espera, el worker lo levanta, no lo encuentra, y desde afuera se ve como un
    import que desaparecio sin que nadie lo borrara.
    """
    from smart_import.job_store_redis import BUSY_TTL_FLOOR_S

    redis = FakeRedis()
    # 1 hora de retencion: mucho mas corta que lo que puede esperar en la cola
    store = RedisJobStore(redis, prefix="t:", ttl_s=3600)

    job = store.create("entregas.xlsx", "vepathos_flat_v1")
    job.status = ANALYZING                       # encolado, esperando un worker
    store.save(job)
    assert job.busy is True
    _, vence_en_vuelo = redis._valores[f"t:job:{job.id}"]

    job.status = NORMALIZED                      # ya esta el resultado
    store.save(job)
    assert job.busy is False
    _, vence_terminado = redis._valores[f"t:job:{job.id}"]

    # En vuelo aguanta el piso; terminado, la retencion corta que se configuro.
    assert vence_en_vuelo - time.time() >= BUSY_TTL_FLOOR_S - 5
    assert vence_terminado - time.time() < BUSY_TTL_FLOOR_S


def test_terminar_acorta_el_ttl_sin_que_nadie_barra_nada():
    """La transicion a terminal es la que empieza a caducar el job."""
    redis = FakeRedis()
    store = RedisJobStore(redis, prefix="t:", ttl_s=3600)
    job = store.create("x.csv", "vepathos_flat_v1")
    job.status = GEOCODING
    store.save(job)
    largo = redis._valores[f"t:job:{job.id}"][1]

    job.status = COMPLETED
    store.save(job)
    assert redis._valores[f"t:job:{job.id}"][1] < largo
