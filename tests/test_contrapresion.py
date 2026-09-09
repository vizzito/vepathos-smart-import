"""La tercera puerta: cuando la flota no da abasto, se dice.

Con todo en un proceso, un servicio saturado contestaba 429 "reintenta en 20 s"
y el operador lo veia. Mandando el trabajo a la cola esa senal se pierde: la api
puede aceptar imports mucho mas rapido de lo que la flota los consume, y sin
techo eso NO falla — se acumula. El usuario no recibe ningun error, solo un
import que tarda diez minutos, y del lado del servidor no hay nada raro para
mirar salvo una cola larga que nadie esta mirando.

Esta puerta repone esa senal. Se mide contra la foto de la flota, no
preguntandole al broker en cada request: es una valvula gruesa, y a esta altura
del archivo ya esta demostrado que meter I/O en el camino del request es como se
rompe /health.
"""
import importlib

import pytest
from fastapi.testclient import TestClient

from smart_import.fleet import config_digest
from smart_import.jobs import JobStore
from smart_import.queue.envelope import normalize_task
from tests.conftest import FIXTURES
from tests.fake_broker import FakeBroker

api_module = importlib.import_module("smart_import.api.app")


@pytest.fixture
def api_con_cola(tmp_path, monkeypatch):
    """Rol `api` con un broker y una foto de flota que el test controla."""
    cfg = api_module.CFG.replace(role="api", work_dir=str(tmp_path),
                                 default_wait_s=0.0, max_queue_depth=10)
    monkeypatch.setattr(api_module, "CFG", cfg)
    monkeypatch.setattr(api_module, "store", JobStore(tmp_path))
    monkeypatch.setattr(api_module, "artifacts", api_module.make_artifact_store(cfg))
    broker = FakeBroker()
    monkeypatch.setattr(api_module, "broker", broker)

    def foto(esperando: int, workers=()):
        for _ in range(esperando):
            broker.publish(normalize_task("imp_relleno"))
        monkeypatch.setattr(api_module, "_fleet_scan", (0.0, {
            "role": "api", "config_digest": config_digest(cfg),
            "workers": list(workers), "queues": broker.depths(),
            "config_drift": [],
        }))

    return TestClient(api_module.app), foto, cfg


def _subir(client):
    with open(FIXTURES / "es_sin_coords.csv", "rb") as fh:
        return client.post("/imports",
                           files={"file": ("es_sin_coords.csv", fh.read())})


def test_con_la_cola_corta_se_acepta(api_con_cola):
    client, foto, _ = api_con_cola
    foto(esperando=3)
    assert _subir(client).status_code in (201, 202)


def test_con_la_cola_pasada_del_techo_se_rechaza_con_429(api_con_cola):
    client, foto, _ = api_con_cola
    foto(esperando=11)                      # techo = 10
    res = _subir(client)
    assert res.status_code == 429
    assert "11" in res.json()["detail"] and "10" in res.json()["detail"]


def test_el_429_trae_retry_after(api_con_cola):
    """Sin `Retry-After` el front no sabe cada cuanto reintentar y martilla."""
    client, foto, _ = api_con_cola
    foto(esperando=11)
    res = _subir(client)
    assert int(res.headers["Retry-After"]) > 0


def test_el_rechazo_no_deja_el_archivo_en_disco(api_con_cola, tmp_path):
    """Se rechaza ANTES de leer el body, igual que la puerta de admision.

    Si no, una avalancha deja un archivo por intento en el disco de la api hasta
    que pase el TTL — que es justo lo que la cola llena esta diciendo que no hay
    capacidad para procesar.
    """
    client, foto, _ = api_con_cola
    foto(esperando=50)
    assert _subir(client).status_code == 429
    assert list(tmp_path.glob("imp_*")) == []


def test_en_cero_no_hay_techo(api_con_cola, monkeypatch):
    """`0` = aceptar siempre, para quien prefiera que la cola absorba todo."""
    client, foto, cfg = api_con_cola
    monkeypatch.setattr(api_module, "CFG", cfg.replace(max_queue_depth=0))
    foto(esperando=500)
    assert _subir(client).status_code in (201, 202)


def test_sin_foto_de_flota_todavia_no_se_rechaza(api_con_cola, monkeypatch):
    """Arrancando, la foto no existe: aceptar es mejor que rechazar a ciegas."""
    client, foto, _ = api_con_cola
    foto(esperando=99)
    monkeypatch.setattr(api_module, "_fleet_scan", None)
    assert _subir(client).status_code in (201, 202)


def test_en_embedded_la_puerta_no_existe(tmp_path, monkeypatch):
    """Sin broker no hay cola que medir: manda el techo de CPU de siempre."""
    cfg = api_module.CFG.replace(role="embedded", work_dir=str(tmp_path),
                                 max_queue_depth=1)
    monkeypatch.setattr(api_module, "CFG", cfg)
    monkeypatch.setattr(api_module, "store", JobStore(tmp_path))
    monkeypatch.setattr(api_module, "artifacts", api_module.make_artifact_store(cfg))
    monkeypatch.setattr(api_module, "broker", None)
    monkeypatch.setattr(api_module, "_fleet_scan", None)
    assert _subir(TestClient(api_module.app)).status_code == 201
