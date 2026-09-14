"""Un worker sin los archivos del usuario tiene que poder hacer el trabajo igual.

Estos tests corren el `HttpArtifactStore` contra la API DE VERDAD (el router
`/internal` montado en la app real, alcanzado en proceso por ASGI): lo que se
verifica no es que dos clases hagan lo mismo, sino que los bytes que escribe un
proceso los sirva el otro, que es la unica pregunta que importa.

Lo que se prueba:
  - paridad: el mismo artefacto, por local y por HTTP, da los mismos bytes;
  - la referencia que devuelve el worker la resuelve la api (no es una ruta);
  - sin token no hay `/internal`, ni siquiera para saber que existe;
  - el scratch del worker se borra pase lo que pase.
"""
import importlib

import httpx
import pytest
from fastapi.testclient import TestClient

from smart_import.api.internal import TOKEN_HEADER
from smart_import.artifacts import (
    FLAT, GEOCODED, RAW, ArtifactRejected, ArtifactTransferError,
    HttpArtifactStore, LocalArtifactStore, PARCIAL, artifact_uri,
)
from smart_import.jobs import JobStore

api_module = importlib.import_module("smart_import.api.app")

TOKEN = "un-token-compartido"
API_URL = "http://api-de-mentira"
CONTENIDO = b"id,address\n1,Av Rivadavia 100\n"


@pytest.fixture
def api(tmp_path, monkeypatch):
    """El rol api: su disco, su store, su token."""
    cfg = api_module.CFG.replace(work_dir=str(tmp_path / "api"), worker_token=TOKEN)
    artefactos = LocalArtifactStore(cfg.work_dir)
    store = JobStore(tmp_path / "api")
    monkeypatch.setattr(api_module, "CFG", cfg)
    monkeypatch.setattr(api_module, "artifacts", artefactos)
    monkeypatch.setattr(api_module, "store", store)
    return cfg, store, artefactos


def _cliente_contra_la_api():
    """Un cliente HTTP real, sin socket: habla ASGI con la app de verdad.

    El `TestClient` es un `httpx.Client` con el transporte apuntado a la app, asi
    que el almacen no se entera de que del otro lado no hay red: pasan por el
    mismo codigo las cabeceras, los codigos de estado y el streaming del body.
    """
    return TestClient(api_module.app, base_url=API_URL)


@pytest.fixture
def worker(api, tmp_path):
    """El rol worker: otro disco, ningun archivo, solo HTTP contra la api."""
    with _cliente_contra_la_api() as cliente:
        yield HttpArtifactStore(API_URL, TOKEN, tmp_path / "scratch", client=cliente)


@pytest.fixture
def http_crudo(api):
    """Cliente pelado, para probar el router sin el almacen del medio."""
    with _cliente_contra_la_api() as cliente:
        yield cliente


def _job_con_raw(api, nombre="entregas.csv", datos=CONTENIDO):
    _, store, artefactos = api
    job = store.create(nombre, "vepathos")
    path = artefactos.reserve(job.id, RAW, filename=nombre)
    path.write_bytes(datos)
    job.raw_path = artefactos.publish(job.id, RAW, path)
    store.save(job)
    return job


# --------------------------------------------------------------- paridad

def test_lo_que_publica_el_worker_lo_sirve_la_api(api, worker):
    """El ida y vuelta completo: el worker escribe en SU disco y la api lo tiene."""
    _, store, artefactos = api
    job = _job_con_raw(api)
    salida = worker.reserve(job.id, FLAT)
    salida.write_bytes(b"id,lat,lng\n1,-34.6,-58.4\n")

    ref = worker.publish(job.id, FLAT, salida)

    assert ref == artifact_uri(job.id, FLAT), "la ruta del worker no significa nada alla"
    servido = artefactos.resolve(job.id, FLAT, ref)
    assert servido is not None, "la api no encontro lo que subio el worker"
    assert servido.read_bytes() == b"id,lat,lng\n1,-34.6,-58.4\n"
    assert servido.parent.parent == artefactos.dir_for(job.id), "quedo fuera del layout"


def test_local_y_http_dan_los_mismos_bytes(api, worker, tmp_path):
    """Paridad: cambiar de backend no cambia el contenido de un artefacto."""
    job = _job_con_raw(api)
    _, _, artefactos = api

    por_http = worker.reserve(job.id, GEOCODED)
    por_http.write_bytes(CONTENIDO)
    ref_http = worker.publish(job.id, GEOCODED, por_http)

    otro = LocalArtifactStore(tmp_path / "otra-api")
    por_local = otro.reserve(job.id, GEOCODED)
    por_local.write_bytes(CONTENIDO)
    ref_local = otro.publish(job.id, GEOCODED, por_local)

    assert (artefactos.resolve(job.id, GEOCODED, ref_http).read_bytes()
            == otro.resolve(job.id, GEOCODED, ref_local).read_bytes()
            == CONTENIDO)


def test_la_referencia_remota_no_rompe_la_descarga(api, worker):
    """El endpoint publico sirve un archivo que produjo otra maquina.

    Es el punto de todo esto: `job.normalized_path` deja de ser una ruta y la
    descarga tiene que seguir funcionando igual.
    """
    _, store, _ = api
    job = _job_con_raw(api)
    salida = worker.reserve(job.id, FLAT)
    salida.write_bytes(CONTENIDO)
    job.normalized_path = worker.publish(job.id, FLAT, salida)
    store.save(job)

    with _cliente_contra_la_api() as publico:
        r = publico.get(f"/imports/{job.id}/download", params={"format": "normalized"})
    assert r.status_code == 200
    assert r.content == CONTENIDO
    assert "normalized.csv" in r.headers["content-disposition"]


def test_un_nombre_con_ruta_no_escapa_del_scratch(api, worker, monkeypatch):
    """El nombre del archivo llega por la red: no puede elegir donde se escribe."""
    monkeypatch.setattr("smart_import.artifacts.http._nombre_servido",
                        lambda r: "../../../../tmp/escapado.csv")
    job = _job_con_raw(api)

    bajado = worker.resolve(job.id, RAW, job.raw_path)

    assert bajado.name == "escapado.csv"
    assert worker.dir_for(job.id) in bajado.parents


def test_el_worker_baja_el_raw_con_su_extension(api, worker):
    """Sin extension no hay lector: el pipeline elige parser por el nombre."""
    job = _job_con_raw(api, nombre="entregas_marzo.xlsx", datos=b"PK\x03\x04 falso")

    bajado = worker.resolve(job.id, RAW, job.raw_path)

    assert bajado is not None
    assert bajado.suffix == ".xlsx", f"se bajo como {bajado.name}"
    assert bajado.read_bytes() == b"PK\x03\x04 falso"
    assert worker.dir_for(job.id) in bajado.parents, "escribio fuera del scratch"


def test_lo_ya_bajado_no_se_vuelve_a_bajar(api, worker):
    """Dentro de una tarea el mismo artefacto se pide varias veces."""
    _, _, artefactos = api
    job = _job_con_raw(api)
    primero = worker.resolve(job.id, RAW, job.raw_path)

    artefactos.delete(job.id)               # si volviera a la red, seria 404
    assert worker.resolve(job.id, RAW, job.raw_path) == primero


def test_pedir_algo_que_no_existe_es_none_y_no_una_excepcion(api, worker):
    """Preguntar si un artefacto esta es normal: `exists()` lo hace todo el tiempo."""
    job = _job_con_raw(api)
    assert worker.resolve(job.id, GEOCODED, None) is None
    assert worker.exists(job.id, GEOCODED, None) is False


def test_publicar_para_un_job_inexistente_falla_sin_reintentar(api, worker):
    """Un 4xx no se arregla esperando; la tarea vuelve a la cola enseguida."""
    salida = worker.reserve("imp_fantasma", FLAT)
    salida.write_bytes(CONTENIDO)

    with pytest.raises(ArtifactRejected):
        worker.publish("imp_fantasma", FLAT, salida)


def test_un_error_pasajero_se_reintenta(api, tmp_path, monkeypatch):
    """La api reiniciandose por un deploy no puede costar una tarea entera."""
    monkeypatch.setattr("smart_import.artifacts.http.ESPERA_BASE_S", 0.0)
    intentos = []

    class ClienteQueFallaAlPrincipio:
        def __init__(self, real):
            self._real = real

        def put(self, *args, **kwargs):
            intentos.append(1)
            if len(intentos) < 3:
                raise httpx.ConnectError("connection refused")
            return self._real.put(*args, **kwargs)

    job = _job_con_raw(api)
    with _cliente_contra_la_api() as real:
        almacen = HttpArtifactStore(API_URL, TOKEN, tmp_path / "s",
                                    client=ClienteQueFallaAlPrincipio(real))
        salida = almacen.reserve(job.id, FLAT)
        salida.write_bytes(CONTENIDO)
        assert almacen.publish(job.id, FLAT, salida) == artifact_uri(job.id, FLAT)
    assert len(intentos) == 3


def test_si_no_afloja_se_avisa_como_falla_de_transporte(api, tmp_path, monkeypatch):
    monkeypatch.setattr("smart_import.artifacts.http.ESPERA_BASE_S", 0.0)

    class ClienteMuerto:
        def put(self, *args, **kwargs):
            raise httpx.ConnectError("connection refused")

    job = _job_con_raw(api)
    almacen = HttpArtifactStore(API_URL, TOKEN, tmp_path / "s",
                                client=ClienteMuerto())
    salida = almacen.reserve(job.id, FLAT)
    salida.write_bytes(CONTENIDO)
    with pytest.raises(ArtifactTransferError, match="no se pudo publicar"):
        almacen.publish(job.id, FLAT, salida)


# --------------------------------------------------------------- token

def test_sin_token_en_el_request_el_router_no_existe(api, http_crudo):
    job = _job_con_raw(api)
    r = http_crudo.get(f"/internal/jobs/{job.id}/artifacts/raw")
    assert r.status_code == 404, "el router se dejo ver sin credencial"


def test_con_el_token_equivocado_tampoco(api, http_crudo):
    job = _job_con_raw(api)
    r = http_crudo.get(f"/internal/jobs/{job.id}/artifacts/raw",
                       headers={TOKEN_HEADER: TOKEN + "x"})
    assert r.status_code == 404


def test_sin_token_configurado_el_router_esta_cerrado(api, http_crudo, monkeypatch):
    """Un despliegue que se olvido la variable queda cerrado, no abierto."""
    cfg, _, _ = api
    monkeypatch.setattr(api_module, "CFG", cfg.replace(worker_token=""))
    job = _job_con_raw(api)

    for cabeceras in ({}, {TOKEN_HEADER: ""}, {TOKEN_HEADER: TOKEN}):
        r = http_crudo.get(f"/internal/jobs/{job.id}/artifacts/raw", headers=cabeceras)
        assert r.status_code == 404, f"paso con {cabeceras}"


def test_con_el_token_correcto_se_sirve(api, http_crudo):
    job = _job_con_raw(api)
    r = http_crudo.get(f"/internal/jobs/{job.id}/artifacts/raw",
                       headers={TOKEN_HEADER: TOKEN})
    assert r.status_code == 200
    assert r.content == CONTENIDO


def test_el_router_interno_no_se_publica(api, http_crudo):
    """No es API: no tiene por que aparecer en el contrato que lee un cliente."""
    esquema = http_crudo.get("/openapi.json").json()
    assert not [ruta for ruta in esquema["paths"] if ruta.startswith("/internal")]


def test_un_kind_inventado_no_llega_al_disco(api, http_crudo):
    job = _job_con_raw(api)
    r = http_crudo.put(f"/internal/jobs/{job.id}/artifacts/..%2F..%2Fetc",
                       headers={TOKEN_HEADER: TOKEN}, content=b"x")
    assert r.status_code == 404


def test_el_archivo_del_usuario_no_se_puede_pisar(api, http_crudo):
    """El raw es la unica entrada que no se regenera: `PUT /mapping` sale de ahi."""
    _, _, artefactos = api
    job = _job_con_raw(api)

    r = http_crudo.put(f"/internal/jobs/{job.id}/artifacts/raw",
                       headers={TOKEN_HEADER: TOKEN}, content=b"otra cosa")

    assert r.status_code == 409
    assert artefactos.resolve(job.id, RAW, job.raw_path).read_bytes() == CONTENIDO


def test_no_se_escribe_para_un_job_que_no_existe(api, http_crudo):
    r = http_crudo.put("/internal/jobs/imp_fantasma/artifacts/flat",
                       headers={TOKEN_HEADER: TOKEN}, content=CONTENIDO)
    assert r.status_code == 404


# --------------------------------------------------------------- scratch

def test_la_subida_no_deja_archivos_a_medias(api, worker):
    """Un parcial con el nombre del bueno es peor que no tener nada."""
    _, _, artefactos = api
    job = _job_con_raw(api)
    salida = worker.reserve(job.id, FLAT)
    salida.write_bytes(CONTENIDO)
    worker.publish(job.id, FLAT, salida)

    sobrantes = [p.name for p in artefactos.dir_for(job.id).rglob(f"*{PARCIAL}")]
    assert sobrantes == []


@pytest.mark.parametrize("como_termina", ["bien", "excepcion", "timeout"])
def test_el_scratch_se_borra_siempre(worker, como_termina):
    """Un worker de larga vida que acumula archivos se queda sin disco."""
    job_id = "imp_lo_que_sea"

    def trabajar():
        with worker.trabajando_en(job_id) as carpeta:
            worker.reserve(job_id, FLAT).write_bytes(CONTENIDO)
            assert any(carpeta.rglob("*")), "el fixture no escribio nada"
            if como_termina == "excepcion":
                raise RuntimeError("el normalize exploto")
            if como_termina == "timeout":
                raise TimeoutError("la tarea tardo demasiado")

    if como_termina == "bien":
        trabajar()
    else:
        with pytest.raises((RuntimeError, TimeoutError)):
            trabajar()

    assert not worker.dir_for(job_id).exists(), "quedo basura en el scratch"


def test_borrar_en_el_worker_no_borra_lo_que_sirve_la_api(api, worker):
    """Un worker no decide que se deja de ofrecer: eso es del rol api."""
    _, _, artefactos = api
    job = _job_con_raw(api)
    worker.resolve(job.id, RAW, job.raw_path)

    worker.delete(job.id)

    assert not worker.dir_for(job.id).exists()
    assert artefactos.resolve(job.id, RAW, job.raw_path) is not None


# --------------------------------------------------------------- config

def test_un_worker_sin_api_no_arranca(api):
    """Mejor morir al arrancar que fallar a mitad de la primera tarea."""
    from smart_import.artifacts import make_artifact_store

    cfg, _, _ = api
    with pytest.raises(RuntimeError, match="SMART_IMPORT_API_URL"):
        make_artifact_store(cfg.replace(role="worker", api_url="", worker_token=""))
    with pytest.raises(RuntimeError, match="SMART_IMPORT_WORKER_TOKEN"):
        make_artifact_store(cfg.replace(role="worker", api_url="http://api",
                                        worker_token=""))


def test_el_worker_usa_http_y_el_resto_disco(api, tmp_path):
    from smart_import.artifacts import make_artifact_store

    cfg, _, _ = api
    remoto = make_artifact_store(cfg.replace(role="worker", api_url="http://api",
                                             worker_token=TOKEN,
                                             scratch_dir=str(tmp_path / "s")))
    assert isinstance(remoto, HttpArtifactStore)
    for rol in ("embedded", "api"):
        assert isinstance(make_artifact_store(cfg.replace(role=rol)),
                          LocalArtifactStore)


# ------------------------------------------------------- techo de un artefacto

def test_un_artefacto_gigante_se_corta_al_vuelo(api, http_crudo, monkeypatch):
    """El disco de la api es el mismo donde corre routehub.

    La puerta publica ya acota lo que ENTRA, pero la salida del pipeline puede
    ser mucho mas grande que su entrada: 50k filas con `diagnostics=true` son
    varias veces el .xlsx original. Sin techo, un worker con un bug escribe
    hasta llenar la particion, y el error lo termina dando el filesystem cuando
    ya es tarde para todo lo demas que corre en esa VM.
    """
    cfg, store, artefactos = api
    monkeypatch.setattr(api_module, "CFG", cfg.replace(max_artifact_mb=0.001))
    job = _job_con_raw(api)

    res = http_crudo.put(f"/internal/jobs/{job.id}/artifacts/{FLAT}",
                         content=b"x" * 5000, headers={TOKEN_HEADER: TOKEN})

    assert res.status_code == 413
    assert "MAX_ARTIFACT_MB" in res.json()["detail"]


def test_el_artefacto_rechazado_no_deja_nada_en_disco(api, http_crudo, monkeypatch):
    """Ni el archivo bueno ni el parcial: cortar y dejar basura es peor."""
    cfg, store, artefactos = api
    monkeypatch.setattr(api_module, "CFG", cfg.replace(max_artifact_mb=0.001))
    job = _job_con_raw(api)

    http_crudo.put(f"/internal/jobs/{job.id}/artifacts/{FLAT}",
                   content=b"x" * 5000, headers={TOKEN_HEADER: TOKEN})

    quedaron = [p for p in artefactos.dir_for(job.id).rglob("*")
                if p.is_file() and p.name != "entregas.csv"]
    assert quedaron == [], quedaron


def test_un_artefacto_normal_pasa(api, http_crudo):
    """El techo es generoso: rechazar un resultado legitimo obliga a rehacerlo."""
    job = _job_con_raw(api)
    res = http_crudo.put(f"/internal/jobs/{job.id}/artifacts/{FLAT}",
                         content=CONTENIDO, headers={TOKEN_HEADER: TOKEN})
    assert res.status_code == 204


def test_intentos_publican_artefactos_aislados_y_rechazan_lease_vencido(api, worker):
    from smart_import.execution import Execution, executing
    _, store, local = api
    job = _job_con_raw(api)
    first, second = 'a'*32, 'b'*32
    assert store.claim_run(job.id, first, 60)
    with executing(Execution(job.id, first)):
        path = worker.reserve(job.id, FLAT)
        path.write_bytes(b'first')
        first_ref = worker.publish(job.id, FLAT, path)
        store.release_run(job.id, first)
        assert store.claim_run(job.id, second, 60)
        path.write_bytes(b'late')
        with pytest.raises(ArtifactRejected):
            worker.publish(job.id, FLAT, path)
    with executing(Execution(job.id, second)):
        path = worker.reserve(job.id, FLAT)
        path.write_bytes(b'second')
        second_ref = worker.publish(job.id, FLAT, path)
    assert first_ref != second_ref
    assert local.resolve(job.id, FLAT, first_ref).read_bytes() == b'first'
    assert local.resolve(job.id, FLAT, second_ref).read_bytes() == b'second'
