"""Ninguna escritura del job puede depender de que el lector vea el MISMO objeto.

Este es el alambre-trampa de la migracion a estado distribuido, y prueba algo
que hoy no se puede romper en produccion: mientras el store es un `dict` en
memoria, `store.get()` devuelve el objeto que todos comparten, asi que mutarlo
alcanza para que el cambio se vea. Un store remoto devuelve una COPIA, y esa
misma mutacion se pierde sin error, sin log y sin test en rojo — el sintoma es un
job que se queda "geocodificando" para siempre.

La forma de adelantar ese futuro sin montar Redis es correr los endpoints de
siempre contra un store que copia en cada lectura. Si algun camino se olvido de
`store.save(job)`, aca se ve como un estado que no avanza.
"""
import copy
import importlib
import time

import pytest
from fastapi.testclient import TestClient

from smart_import.api.app import app
from smart_import.jobs import Job, JobStore
from tests.conftest import FIXTURES

api_module = importlib.import_module("smart_import.api.app")


class StoreQueCopia(JobStore):
    """Lo que hace un backend remoto: entregar copias, no referencias.

    Es la implementacion mas barata de la semantica de Redis. `save` sigue
    siendo el del padre (pisa la entrada del dict), asi que lo unico que cambia
    respecto de produccion es que el aliasing deja de tapar los olvidos.
    """

    def create(self, filename: str, schema: str) -> Job:
        return copy.deepcopy(super().create(filename, schema))

    def get(self, job_id: str) -> Job | None:
        job = super().get(job_id)
        return copy.deepcopy(job) if job is not None else None


@pytest.fixture
def client_con_copias(tmp_path, monkeypatch):
    """La API completa, contra un store que NO comparte objetos con ella."""
    cfg = api_module.CFG.replace(geocoding_enabled=True, pbf_dir="/tmp/pbf",
                                 work_dir=str(tmp_path))
    artefactos = api_module.make_artifact_store(cfg)

    monkeypatch.setattr(api_module, "CFG", cfg)
    monkeypatch.setattr(api_module, "store", StoreQueCopia(tmp_path))
    monkeypatch.setattr(api_module, "artifacts", artefactos)
    return TestClient(app)


def _subir(client, nombre="es_sin_coords.csv", **params):
    with open(FIXTURES / nombre, "rb") as fh:
        return client.post("/imports", files={"file": (nombre, fh.read())},
                           params=params)


def test_el_resultado_del_normalize_queda_guardado(client_con_copias):
    """Si `run_normalize_job` no guarda, el GET devuelve el job recien subido."""
    creado = _subir(client_con_copias).json()
    assert creado["status"] in ("normalized", "needs_mapping_review")

    leido = client_con_copias.get(f"/imports/{creado['job_id']}").json()
    assert leido["status"] == creado["status"], "el normalize no se guardo"
    assert leido["report"]["rows_output"] == creado["report"]["rows_output"]
    assert "download" in {a["action"] for a in leido["next_actions"]}


def test_lo_que_produjo_el_normalize_lo_sirve_otro_request(client_con_copias):
    """Descargar y previsualizar son requests APARTE del que normalizo.

    Los archivos los encuentra igual (el almacen de artefactos sabe el layout),
    pero el mapping vive solo en el `Job`: si el report no se guardo, la UI
    dibuja la tabla sin una sola columna reconocida.
    """
    job_id = _subir(client_con_copias).json()["job_id"]

    assert client_con_copias.get(f"/imports/{job_id}/download",
                                 params={"format": "flat"}).status_code == 200
    assert client_con_copias.get(f"/imports/{job_id}/download",
                                 params={"format": "nested"}).status_code == 200

    preview = client_con_copias.get(f"/imports/{job_id}/preview").json()
    assert preview["rows"], "el preview no devolvio filas"
    assert preview["mapping"], "el mapping se perdio entre el normalize y la lectura"


def test_el_remapeo_manual_tambien_se_guarda(client_con_copias):
    job_id = _subir(client_con_copias, "es_headers_raros.xlsx").json()["job_id"]

    r = client_con_copias.put(f"/imports/{job_id}/mapping",
                              json={"Dest.": "address"})
    assert r.status_code == 200

    leido = client_con_copias.get(f"/imports/{job_id}").json()
    assert leido["report"]["mapping"]["Dest."]["method"] == "manual"


def test_la_reserva_del_geocode_no_la_pisa_el_request_que_la_hizo(
        client_con_copias, monkeypatch):
    """`claim_geocode` escribe en el store; el request tenia una copia PREVIA.

    Guardar esa copia despues del claim devuelve el job a NORMALIZED y el
    siguiente POST (doble click, retry del front) lanza un segundo worker sobre
    el mismo CSV. El 409 de la segunda llamada es lo que prueba que no pasa.

    El worker no corre a proposito: si corriera, fallaria por falta de PBF y
    liberaria el job, y el resultado del test pasaria a depender de quien gana
    la carrera.
    """
    class SinWorker:
        def submit(self, *_a, **_kw) -> None:
            return None

    monkeypatch.setattr(api_module, "_geocode_pool", SinWorker())
    job_id = _subir(client_con_copias).json()["job_id"]

    primera = client_con_copias.post(f"/imports/{job_id}/geocode",
                                     params={"origin_lat": -34.6, "origin_lon": -58.4})
    assert primera.status_code == 202
    assert primera.json()["busy"] is True

    segunda = client_con_copias.post(f"/imports/{job_id}/geocode",
                                     params={"origin_lat": -34.6, "origin_lon": -58.4})
    assert segunda.status_code == 409


def test_el_geocode_que_falla_deja_el_job_en_un_estado_terminal(client_con_copias):
    """Sin PBF el worker falla, y ESE final tiene que quedar guardado.

    Es el caso que mas duele si se pierde: el job queda `busy` para siempre, la
    UI pollea sin parar y el usuario no puede ni reintentar ni ubicar a mano.
    """
    job_id = _subir(client_con_copias).json()["job_id"]
    client_con_copias.post(f"/imports/{job_id}/geocode",
                           params={"origin_lat": -34.6, "origin_lon": -58.4})

    limite = time.time() + 30
    while time.time() < limite:
        cuerpo = client_con_copias.get(f"/imports/{job_id}").json()
        if not cuerpo["busy"]:
            break
        time.sleep(0.05)

    assert cuerpo["busy"] is False, "el job quedo ocupado: el worker no guardo el final"
    assert cuerpo["status"] == "geocode_failed"
    assert cuerpo["error"], "sin error guardado la UI no sabe que decirle al usuario"
    assert "manual_geocode" in {a["action"] for a in cuerpo["next_actions"]}
