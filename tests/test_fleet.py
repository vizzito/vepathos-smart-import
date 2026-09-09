"""La flota: quien esta trabajando, y si estan todos configurados igual.

Dos propiedades distintas, con costos muy distintos si fallan.

La primera es visible: si el latido no funciona, `/health` no lista workers y el
operador no sabe si prendio algo. Molesta, pero se nota.

La segunda es la cara. El worker ESTAMPA las bandas de geocode en el CSV y la
api las REPINTA para la UI con su propio `Config`. Si los dos no tienen los
mismos umbrales nadie recibe un error: el operador ve verde donde el archivo
dice ambar y confia en un pin que no deberia. Por eso el latido lleva una huella
de la config compartida, y por eso el digest tiene tests propios de que cambia
con lo que importa y NO cambia con lo que legitimamente difiere por maquina.
"""
import json

import pytest

from smart_import.config import Config
from smart_import.fleet import (
    CAMPOS_COMPARTIDOS, Heartbeat, config_digest, drift, node_id, read_fleet,
)
from tests.fake_redis import FakeRedis


@pytest.fixture
def cfg():
    return Config.from_env().replace(role="worker", redis_prefix="t:",
                                     node_heartbeat_ttl_s=90, worker_slots=3)


# ----------------------------------------------------------------- digest

def test_el_digest_cambia_si_cambia_una_banda(cfg):
    """Lo que define como se pinta un resultado ENTRA en la huella."""
    assert config_digest(cfg.replace(geocode_valid_band=0.99)) != config_digest(cfg)


def test_el_digest_no_cambia_con_lo_que_es_propio_de_cada_maquina(cfg):
    """Slots, PBFs y rol son legitimamente distintos en cada nodo.

    Si entraran en la huella, TODA la flota apareceria en drift permanente y el
    aviso dejaria de significar algo — que es la forma habitual en que un chequeo
    asi se vuelve ruido y se termina ignorando.
    """
    igual = cfg.replace(worker_slots=99, pbf_dir="/otro/lado", role="api",
                        consume_geocode=not cfg.consume_geocode)
    assert config_digest(igual) == config_digest(cfg)


def test_el_digest_cubre_todos_los_campos_declarados(cfg):
    """Ningun campo de la lista puede estar de adorno."""
    base = config_digest(cfg)
    for campo in CAMPOS_COMPARTIDOS:
        actual = getattr(cfg, campo)
        distinto = (not actual) if isinstance(actual, bool) else (
            f"{actual}-x" if isinstance(actual, str) else float(actual) + 1)
        assert config_digest(cfg.replace(**{campo: distinto})) != base, campo


# ----------------------------------------------------------------- latido

def test_el_latido_publica_el_nodo_con_ttl(cfg):
    redis = FakeRedis()
    latido = Heartbeat(redis, cfg, colas=["smart-import-normalize"])
    assert latido.beat() is True

    guardado = json.loads(redis.get(f"t:node:{node_id()}"))
    assert guardado["role"] == "worker"
    assert guardado["queues"] == ["smart-import-normalize"]
    assert guardado["slots"] == 3
    assert guardado["config_digest"] == config_digest(cfg)


def test_el_nodo_desaparece_solo_cuando_deja_de_latir(cfg):
    """El TTL es el interruptor: nadie da de baja un worker a mano."""
    redis = FakeRedis()
    Heartbeat(redis, cfg, colas=[]).beat()
    clave = f"t:node:{node_id()}"
    assert read_fleet(redis, "t:")

    redis.vencer(clave)
    assert read_fleet(redis, "t:") == []


def test_stop_baja_el_nodo_sin_esperar_el_ttl(cfg):
    """Un `compose down` limpio no deja un fantasma 90 s en /health."""
    redis = FakeRedis()
    latido = Heartbeat(redis, cfg, colas=[])
    latido.beat()
    latido.stop()
    assert read_fleet(redis, "t:") == []


def test_un_redis_caido_no_tumba_al_worker(cfg):
    """El latido es telemetria, no una condicion para trabajar.

    Al reves seria peor: un hipo de Redis dejaria a la flota entera sin procesar
    nada.
    """
    class RedisRoto:
        def set(self, *a, **k):
            raise ConnectionError("sin Redis")

    assert Heartbeat(RedisRoto(), cfg, colas=[]).beat() is False


def test_el_periodo_entra_varias_veces_en_el_ttl(cfg):
    """Dos latidos fallidos antes de desaparecer: un hipo no lo hace parpadear."""
    latido = Heartbeat(FakeRedis(), cfg, colas=[])
    assert latido._periodo_s * 2 <= latido._ttl_s


# ------------------------------------------------------------------ lectura

def test_read_fleet_ignora_un_nodo_con_estado_ilegible(cfg):
    """Un worker que escribio basura no puede romper el /health de todos."""
    redis = FakeRedis()
    Heartbeat(redis, cfg, colas=[]).beat()
    redis.set("t:node:roto", "{esto no es json")
    assert [n["node"] for n in read_fleet(redis, "t:")] == [node_id()]


def test_read_fleet_no_mira_claves_de_otro_prefijo(cfg):
    redis = FakeRedis()
    Heartbeat(redis, cfg, colas=[]).beat()
    redis.set("otro:node:ajeno", json.dumps({"node": "ajeno"}))
    assert [n["node"] for n in read_fleet(redis, "t:")] == [node_id()]


# -------------------------------------------------------------------- drift

def test_sin_drift_cuando_todos_coinciden(cfg):
    nodos = [{"node": "a", "config_digest": config_digest(cfg)}]
    assert drift(nodos, config_digest(cfg)) == []


def test_el_worker_con_otros_umbrales_queda_marcado(cfg):
    """El caso caro: estampa con una banda y la api pinta con otra."""
    otro = config_digest(cfg.replace(geocode_valid_band=0.5))
    nodos = [{"node": "sano", "config_digest": config_digest(cfg)},
             {"node": "desalineado", "config_digest": otro}]
    assert drift(nodos, config_digest(cfg)) == ["desalineado"]


def test_un_nodo_sin_digest_no_se_reporta_como_drift(cfg):
    """Un worker viejo que todavia no publica huella no es una desalineacion."""
    assert drift([{"node": "viejo"}], config_digest(cfg)) == []


# ------------------------------------------------------- lo que ve /health

def _health(monkeypatch, tmp_path, cfg, *, fleet):
    """`/health` con una foto de flota puesta a mano."""
    import importlib

    from fastapi.testclient import TestClient

    from smart_import.jobs import JobStore

    api_module = importlib.import_module("smart_import.api.app")
    monkeypatch.setattr(api_module, "CFG", cfg)
    monkeypatch.setattr(api_module, "store", JobStore(tmp_path))
    monkeypatch.setattr(api_module, "_health_scan", None)
    monkeypatch.setattr(api_module, "_fleet_scan", fleet)
    return TestClient(api_module.app).get("/health").json()


def test_en_embedded_health_no_habla_de_flota(monkeypatch, tmp_path, cfg):
    """Un solo proceso no tiene flota: mostrar ceros se lee como 'se cayo todo'."""
    body = _health(monkeypatch, tmp_path, cfg.replace(role="embedded"), fleet=None)
    assert "fleet" not in body


def test_health_lista_los_workers_y_lo_encolado(monkeypatch, tmp_path, cfg):
    foto = (0.0, {"role": "api", "config_digest": config_digest(cfg),
                  "workers": [{"node": "mac:1", "queues": ["smart-import-geocode"]}],
                  "queues": {"smart-import-normalize": 4, "smart-import-dlq": 0},
                  "config_drift": []})
    fleet = _health(monkeypatch, tmp_path, cfg.replace(role="api"), fleet=foto)["fleet"]
    assert [w["node"] for w in fleet["workers"]] == ["mac:1"]
    assert fleet["queues"]["smart-import-normalize"] == 4
    assert fleet["config_drift"] == []
    assert fleet["age_s"] >= 0


def test_health_marca_al_worker_desalineado(monkeypatch, tmp_path, cfg):
    """El aviso que no aparece en ningun otro lado."""
    foto = (0.0, {"role": "api", "config_digest": config_digest(cfg),
                  "workers": [{"node": "mac:1"}], "queues": {},
                  "config_drift": ["mac:1"]})
    fleet = _health(monkeypatch, tmp_path, cfg.replace(role="api"), fleet=foto)["fleet"]
    assert fleet["config_drift"] == ["mac:1"]


def test_la_cola_creciendo_sin_workers_se_ve(monkeypatch, tmp_path, cfg):
    """EL sintoma a mirar: la api encola y no hay nadie del otro lado."""
    foto = (0.0, {"role": "api", "config_digest": config_digest(cfg),
                  "workers": [], "queues": {"smart-import-normalize": 120},
                  "config_drift": []})
    fleet = _health(monkeypatch, tmp_path, cfg.replace(role="api"), fleet=foto)["fleet"]
    assert fleet["workers"] == [] and fleet["queues"]["smart-import-normalize"] == 120


# ------------------------------------------------- cuantos procesos se pueden

def test_embedded_sigue_rechazando_varios_workers_de_uvicorn(monkeypatch):
    """Con el estado en memoria, dos procesos se responden 404 entre ellos."""
    from typer.testing import CliRunner

    from smart_import.cli import app as cli_app

    monkeypatch.setenv("SMART_IMPORT_ROLE", "embedded")
    res = CliRunner().invoke(cli_app, ["serve", "--workers", "4"])
    assert res.exit_code == 2
    assert "embedded" in res.output


def test_en_rol_api_varios_workers_son_validos(monkeypatch):
    """Con el estado en Redis, varios procesos es lo que se quiere.

    Se verifica sobre el guard y no levantando uvicorn: lo que se prueba es la
    DECISION, no que el servidor arranque.
    """
    import importlib

    cli = importlib.import_module("smart_import.cli")
    monkeypatch.setenv("SMART_IMPORT_ROLE", "api")
    monkeypatch.setenv("REDIS_HOST", "10.0.0.2")

    llamadas = {}
    monkeypatch.setattr("uvicorn.run",
                        lambda *a, **k: llamadas.update(k), raising=False)
    cli.serve(host="127.0.0.1", port=8100, reload=False, workers=4)
    assert llamadas["workers"] == 4
