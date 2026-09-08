"""El worker corre el mismo codigo que la API y se diferencia solo por el entorno.

Por eso no puede arrancar "a medias": si el archivo de configuracion no se
aplico, este proceso levantaria creyendo ser otra cosa y el sintoma aparece
mucho despues, como jobs que nadie termina. La revision se hace entera antes de
fallar, porque quien configura un nodo nuevo prefiere la lista completa a
descubrir lo que falta de a una corrida por vez.
"""
import pytest

from smart_import.config import Config
from smart_import.worker.main import describir, revisar_configuracion


@pytest.fixture
def cfg_worker(tmp_path):
    """Un worker bien configurado, salvo por lo que cada test rompa."""
    return Config.from_env().replace(
        role="worker", rabbitmq_host="10.0.0.2", redis_host="10.0.0.2",
        api_url="http://10.0.0.2:8100", worker_token="secreto",
        consume_normalize=True, consume_geocode=False,
        work_dir=str(tmp_path), scratch_dir=str(tmp_path / "scratch"))


def test_un_worker_completo_arranca(cfg_worker):
    assert revisar_configuracion(cfg_worker) == []


@pytest.mark.parametrize("campo, pista", [
    ("rabbitmq_host", "RABBITMQ_HOST"),
    ("redis_host", "REDIS_HOST"),
    ("api_url", "SMART_IMPORT_API_URL"),
    ("worker_token", "SMART_IMPORT_WORKER_TOKEN"),
])
def test_sin_alguna_pieza_no_arranca_y_dice_cual(cfg_worker, campo, pista):
    problemas = revisar_configuracion(cfg_worker.replace(**{campo: ""}))

    assert len(problemas) == 1
    assert pista in problemas[0]


def test_se_reportan_todos_los_faltantes_juntos(cfg_worker):
    """De a uno por corrida, configurar un nodo son cuatro deploys fallidos."""
    problemas = revisar_configuracion(
        cfg_worker.replace(rabbitmq_host="", redis_host="", api_url=""))

    assert len(problemas) == 3


@pytest.mark.parametrize("rol", ["embedded", "api"])
def test_el_rol_equivocado_se_detecta(cfg_worker, rol):
    """Es el `.env` que no se aplico: sin esto, el proceso arranca mudo."""
    problemas = revisar_configuracion(cfg_worker.replace(role=rol))

    assert any("SMART_IMPORT_ROLE" in p for p in problemas)


def test_un_worker_que_no_consume_nada_no_sirve(cfg_worker):
    problemas = revisar_configuracion(
        cfg_worker.replace(consume_normalize=False, consume_geocode=False))

    assert any("CONSUME" in p for p in problemas)


# --------------------------------------------------------------- geocode

def test_prometer_geocode_sin_mapas_no_arranca(cfg_worker):
    """Suscribirse sin PBF es peor que no suscribirse: las tareas rebotan.

    El nodo se lleva trabajo que solo puede fallar mientras el que si tiene los
    mapas mira sin hacer nada, y el usuario termina con un geocode en la DLQ.
    """
    problemas = revisar_configuracion(
        cfg_worker.replace(consume_geocode=True, geocoding_enabled=True, pbf_dir=""))

    assert len(problemas) == 1
    assert "PBF" in problemas[0]


def test_prometer_geocode_con_el_geocoding_apagado_tampoco(cfg_worker):
    problemas = revisar_configuracion(
        cfg_worker.replace(consume_geocode=True, geocoding_enabled=False,
                           pbf_dir="/tmp"))

    assert any("GEOCODING_ENABLED" in p for p in problemas)


def test_un_directorio_de_mapas_vacio_se_detecta(cfg_worker, tmp_path):
    """El volumen mal montado da un directorio que existe y no tiene nada."""
    vacio = tmp_path / "sin_pbf"
    vacio.mkdir()

    problemas = revisar_configuracion(
        cfg_worker.replace(consume_geocode=True, geocoding_enabled=True,
                           pbf_dir=str(vacio), extract_dir=str(vacio)))

    assert len(problemas) == 1
    assert "osm.pbf" in problemas[0]


def test_un_nodo_solo_de_normalize_no_necesita_mapas(cfg_worker):
    """Es el caso comun: la mayoria de los nodos no van a tener los PBF."""
    assert revisar_configuracion(
        cfg_worker.replace(consume_geocode=False, pbf_dir="")) == []


# --------------------------------------------------------------- catalogo

def test_el_catalogo_dice_contra_que_habla_este_nodo(cfg_worker):
    """Es lo primero que se mira cuando un job no avanza."""
    texto = "\n".join(describir(cfg_worker))

    assert "worker" in texto
    assert "10.0.0.2" in texto
    assert "normalize" in texto
    assert "http://10.0.0.2:8100" in texto


def test_el_catalogo_no_publica_el_token(cfg_worker):
    """Los logs de un deploy se pegan en un chat: el token no puede estar ahi."""
    assert "secreto" not in "\n".join(describir(cfg_worker))
