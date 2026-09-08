"""El rol del proceso y los secretos que NO pueden salir por `/config`."""
import pytest
from fastapi.testclient import TestClient

from smart_import.api.app import app
from smart_import.config import ROLES, SECRET_FIELDS, Config


def test_el_default_es_el_comportamiento_de_siempre(monkeypatch):
    """Sin ninguna env, el servicio arranca solo: ni broker ni Redis."""
    for env in ("SMART_IMPORT_ROLE", "RABBITMQ_HOST", "REDIS_HOST",
                "SMART_IMPORT_API_URL", "SMART_IMPORT_WORKER_TOKEN"):
        monkeypatch.delenv(env, raising=False)

    cfg = Config.from_env()
    assert cfg.role == "embedded"
    assert cfg.rabbitmq_host == "" and cfg.redis_host == ""


@pytest.mark.parametrize("rol", ROLES)
def test_los_tres_roles_se_leen(monkeypatch, rol):
    monkeypatch.setenv("SMART_IMPORT_ROLE", rol.upper())
    assert Config.from_env().role == rol


def test_un_rol_mal_escrito_no_degrada_a_embedded(monkeypatch):
    """Un `wroker` que arranca contento es un nodo que no consume nada y que
    nadie extraña hasta que la cola se llena."""
    monkeypatch.setenv("SMART_IMPORT_ROLE", "wroker")
    with pytest.raises(ValueError, match="wroker"):
        Config.from_env()


def test_un_nodo_sin_pbf_no_consume_geocode_por_defecto():
    """El default tiene que ser el seguro: suscribirse a `geocode` sin PBF es
    aceptar trabajo que no se puede hacer."""
    assert Config().consume_geocode is False
    assert Config().consume_normalize is True


def test_config_no_publica_los_secretos(monkeypatch):
    """`/config` no pide credenciales: lo que entra ahi es publico."""
    monkeypatch.setenv("SMART_IMPORT_WORKER_TOKEN", "s3cr3to-de-verdad")
    monkeypatch.setenv("REDIS_PASSWORD", "otra-cosa-secreta")

    descripcion = Config.from_env().describe()

    assert descripcion["worker_token"] == "***"
    assert descripcion["redis_password"] == "***"
    assert "s3cr3to-de-verdad" not in str(descripcion)
    # pero se sigue viendo si el .env se aplico o no
    assert descripcion["rabbitmq_password"] == ""


def test_el_endpoint_config_tampoco(monkeypatch):
    import importlib

    api_module = importlib.import_module("smart_import.api.app")
    monkeypatch.setattr(api_module, "CFG",
                        api_module.CFG.replace(worker_token="token-del-worker"))

    cuerpo = TestClient(app).get("/config").json()["config"]

    assert cuerpo["worker_token"] == "***"
    assert "token-del-worker" not in str(cuerpo)


def test_health_dice_donde_vive_el_estado(monkeypatch):
    """Lo que se mira despues de un deploy para saber si el .env se aplico."""
    import importlib

    from smart_import.job_store_redis import RedisJobStore
    from tests.fake_redis import FakeRedis

    api_module = importlib.import_module("smart_import.api.app")
    cliente = TestClient(app)

    api_module._health_scan = None
    assert cliente.get("/health").json()["deployment"] == {
        "role": "embedded", "state": "memory"}

    monkeypatch.setattr(api_module, "CFG", api_module.CFG.replace(role="api"))
    monkeypatch.setattr(api_module, "store", RedisJobStore(FakeRedis()))
    api_module._health_scan = None
    assert cliente.get("/health").json()["deployment"] == {
        "role": "api", "state": "redis"}
    api_module._health_scan = None


def test_un_rol_que_reparte_trabajo_no_arranca_sin_estado_compartido():
    """Degradar a memoria seria peor que no arrancar: la api responderia 404
    para jobs que existen y que otro proceso esta procesando ahora mismo."""
    from smart_import.jobs import make_job_store

    with pytest.raises(ValueError, match="REDIS_HOST"):
        make_job_store(Config().replace(role="worker", redis_host=""))


def test_todos_los_secretos_declarados_existen_en_el_config():
    """Un nombre mal escrito en `SECRET_FIELDS` no redacta nada y no falla."""
    campos = set(Config().describe())
    assert SECRET_FIELDS <= campos
