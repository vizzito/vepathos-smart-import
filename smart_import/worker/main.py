"""El proceso worker: consume tareas y no sirve HTTP.

Corre el mismo codigo que la API en modo `embedded` —los handlers son los
mismos— y se diferencia SOLO por variables de entorno. Por eso lo primero que
hace es verificar que esas variables sean las de un worker: si el archivo de
configuracion no se aplico, este proceso arrancaria creyendo ser otra cosa y el
sintoma aparecería mucho despues, como jobs que nadie termina.

La revision es completa antes de fallar: quien esta configurando un nodo nuevo
prefiere la lista entera de lo que falta a descubrirlo de a una por corrida.
"""
from __future__ import annotations

import sys

from ..artifacts import make_artifact_store
from ..config import Config
from ..jobs import make_job_store
from ..logging_setup import get_logger, setup as setup_logging
from ..queue import Consumer, make_broker
from .handlers import WorkerContext


def revisar_configuracion(cfg: Config) -> list[str]:
    """Todo lo que le impide a este proceso hacer su trabajo, junto."""
    problemas: list[str] = []
    if cfg.role != "worker":
        problemas.append(
            f"SMART_IMPORT_ROLE={cfg.role} — este proceso es un worker; con "
            "'embedded' o 'api' el trabajo lo hace quien sirve HTTP")
    if not cfg.rabbitmq_host:
        problemas.append("RABBITMQ_HOST vacio: no hay cola de donde tomar tareas")
    if not cfg.redis_host:
        problemas.append("REDIS_HOST vacio: sin estado compartido, el avance que "
                         "escriba este nodo no lo ve nadie")
    if not cfg.api_url:
        problemas.append("SMART_IMPORT_API_URL vacio: los archivos de los jobs "
                         "estan en la api y hay que ir a buscarlos")
    if not cfg.worker_token:
        problemas.append("SMART_IMPORT_WORKER_TOKEN vacio: la api responde 404 a "
                         "quien no lo traiga")
    if not cfg.consume_normalize and not cfg.consume_geocode:
        problemas.append("SMART_IMPORT_CONSUME_NORMALIZE y _CONSUME_GEOCODE en "
                         "false: este worker no consumiria ninguna cola")
    problemas.extend(_revisar_geocode(cfg))
    return problemas


def _revisar_geocode(cfg: Config) -> list[str]:
    """Un nodo que dice hacer geocode tiene que poder hacerlo.

    Suscribirse sin los PBF montados es peor que no suscribirse: las tareas se
    reparten a este nodo, fallan, rebotan y terminan en la DLQ mientras el que
    si tiene los mapas mira sin trabajo.
    """
    if not cfg.consume_geocode:
        return []
    if not cfg.geocoding_enabled:
        return ["SMART_IMPORT_CONSUME_GEOCODE=true con "
                "SMART_IMPORT_GEOCODING_ENABLED=false"]
    if not cfg.pbf_dir:
        return ["SMART_IMPORT_CONSUME_GEOCODE=true y SMART_IMPORT_PBF_DIR vacio: "
                "sin mapas no hay con que geolocalizar"]
    try:
        from ..geocoding.extract import scan_registry
        registry = scan_registry(cfg.pbf_dir, cfg.extract_dir)
    except Exception as exc:                                    # pragma: no cover
        return [f"no se pudo leer SMART_IMPORT_PBF_DIR ({cfg.pbf_dir}): {exc}"]
    if not registry.entries:
        return [f"SMART_IMPORT_PBF_DIR={cfg.pbf_dir} no tiene ningun .osm.pbf: "
                "revisá el volumen montado"]
    return []


def describir(cfg: Config) -> list[str]:
    """El catalogo que este nodo imprime al arrancar.

    Es lo primero que se mira en `docker logs` cuando un job no avanza: dice de
    que colas come, contra quien habla y con cuantos mapas cuenta.
    """
    lineas = [
        f"rol            {cfg.role}",
        f"colas          {'normalize ' if cfg.consume_normalize else ''}"
        f"{'geocode' if cfg.consume_geocode else ''}".strip() or "(ninguna)",
        f"slots          {cfg.worker_slots}",
        f"broker         {cfg.rabbitmq_host}:{cfg.rabbitmq_port}{cfg.rabbitmq_vhost}",
        f"estado         redis {cfg.redis_host}:{cfg.redis_port} db {cfg.redis_db}",
        f"archivos       {cfg.api_url}",
        f"scratch        {cfg.scratch_dir or '(temporal del sistema)'}",
    ]
    if cfg.consume_geocode:
        try:
            from ..geocoding.extract import scan_registry
            entradas = scan_registry(cfg.pbf_dir, cfg.extract_dir).entries
            lineas.append(f"mapas          {len(entradas)} .osm.pbf en {cfg.pbf_dir}")
        except Exception:                                       # pragma: no cover
            lineas.append(f"mapas          no se pudo leer {cfg.pbf_dir}")
    return lineas


def main() -> int:
    cfg = Config.from_env()
    setup_logging(verbose=cfg.verbose)
    logger = get_logger()

    problemas = revisar_configuracion(cfg)
    if problemas:
        # A stderr y sin log: esto pasa antes de que exista nada que loguear, y
        # quien lo lee esta mirando la consola de un deploy que no arranco.
        print("no puedo arrancar como worker:", file=sys.stderr)
        for problema in problemas:
            print(f"   • {problema}", file=sys.stderr)
        return 2

    for linea in describir(cfg):
        print(f"  {linea}")

    artifacts = make_artifact_store(cfg)
    store = make_job_store(cfg, artifacts)
    broker = make_broker(cfg, logger)

    def contexto() -> WorkerContext:
        return WorkerContext(cfg=cfg, store=store, artifacts=artifacts, logger=logger)

    Consumer(cfg, broker, contexto, logger).run()
    return 0


if __name__ == "__main__":                                      # pragma: no cover
    raise SystemExit(main())
