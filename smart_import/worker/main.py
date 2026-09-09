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
from ..fleet import Heartbeat
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
    problemas.extend(_revisar_lock(cfg))
    return problemas


def _revisar_lock(cfg: Config) -> list[str]:
    """Un TTL de ejecucion por debajo del piso no acelera nada.

    Es un numero que invita a bajarlo —«esperar para retomar un job es mucho»—
    y lo que pasa debajo del piso no es un failover mas rapido: es que el
    jitter normal de red alcanza para que un nodo VIVO pierda el lock y otro
    empiece el mismo archivo. Se avisa al arrancar en vez de aplicar el piso en
    silencio, para que el que puso el numero sepa que no es el que corre.
    """
    from ..queue.consumer import RUN_LOCK_TTL_MIN_S

    if cfg.run_lock_ttl_s < RUN_LOCK_TTL_MIN_S:
        return [f"SMART_IMPORT_RUN_LOCK_TTL_S={cfg.run_lock_ttl_s:g} esta debajo del "
                f"piso de {RUN_LOCK_TTL_MIN_S:g}s, asi que corre el piso. Mas abajo "
                "no se retoma antes: se duplica trabajo de nodos vivos"]
    return []


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


def _retoma_en(cfg: Config) -> float:
    """Peor caso para que otro nodo tome el trabajo de este si muere.

    Son dos esperas encadenadas: que venza el lock del muerto, y que el que
    espera vuelva a preguntar. Sumarlas es lo unico que responde la pregunta
    real —«¿cuanto tarda un import si se cae un worker?»— y ninguno de los dos
    numeros la contesta solo.
    """
    from ..queue.consumer import DEMORA_OCUPADO_MIN_S, DIVISOR_RENOVACION, RUN_LOCK_TTL_MIN_S

    ttl = max(RUN_LOCK_TTL_MIN_S, cfg.run_lock_ttl_s)
    return ttl + max(DEMORA_OCUPADO_MIN_S, ttl / DIVISOR_RENOVACION)


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
        # Cuanto tarda otro nodo en retomar lo de este si se muere callado. Es
        # el numero que explica un job que "no avanza" justo despues de una
        # caida, asi que va en el catalogo y no escondido en la config.
        f"retoma en      hasta {_retoma_en(cfg):.0f}s si este nodo muere",
    ]
    if cfg.consume_geocode:
        try:
            from ..geocoding.extract import scan_registry
            entradas = scan_registry(cfg.pbf_dir, cfg.extract_dir).entries
            lineas.append(f"mapas          {len(entradas)} .osm.pbf en {cfg.pbf_dir}")
        except Exception:                                       # pragma: no cover
            lineas.append(f"mapas          no se pudo leer {cfg.pbf_dir}")
    return lineas


def verificar_conexiones(cfg: Config) -> list[tuple[str, str | None]]:
    """Toca las tres piezas de las que depende un worker. `None` = anduvo.

    Dar de alta un nodo es, en la practica, adivinar si el tunel esta bien
    armado. Esto lo convierte en una respuesta: cada dependencia se prueba por
    separado, asi el que falla se ve solo, y nada de esto arranca el consumo.
    """
    return [
        ("cola", _probar(lambda: _ping_broker(cfg))),
        ("estado", _probar(lambda: _ping_redis(cfg))),
        ("archivos", _probar(lambda: _ping_api(cfg))),
    ]


def _probar(fn) -> str | None:
    try:
        fn()
        return None
    except Exception as exc:
        # pika levanta excepciones sin texto: "AMQPConnectionError: " no le
        # dice nada a nadie. El tipo solo ya es mas informacion que el vacio.
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _ping_broker(cfg: Config) -> None:
    from ..queue.broker import RabbitBroker

    # Un intento y listo: esto es un diagnostico interactivo, no el arranque
    # del servicio. Los 15 s de reintentos que le sirven a un worker que
    # arranca junto al broker aca son solo espera.
    broker = RabbitBroker(cfg, intentos_conexion=1)
    try:
        # Declarar la topologia es la prueba util: verifica credenciales,
        # vhost y permisos, no solo que el puerto conteste.
        broker._conectar()
    finally:
        broker.close()


def _ping_redis(cfg: Config) -> None:
    store = make_job_store(cfg)
    store._client.ping()


def _ping_api(cfg: Config) -> None:
    import httpx

    r = httpx.get(f"{cfg.api_url.rstrip('/')}/internal/health",
                  headers={"X-Smart-Import-Token": cfg.worker_token}, timeout=10)
    if r.status_code == 404:
        # 404 es la respuesta de `/internal` a quien no trae el token bueno:
        # no confirma ni que la ruta existe. Desde aca, es el token.
        raise RuntimeError("la api respondio 404: el token no coincide con el suyo")
    r.raise_for_status()


def main(solo_verificar: bool = False) -> int:
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

    if solo_verificar:
        fallas = 0
        for pieza, error in verificar_conexiones(cfg):
            print(f"  {pieza:<14} {'ok' if error is None else error}")
            fallas += error is not None
        return 1 if fallas else 0

    artifacts = make_artifact_store(cfg)
    store = make_job_store(cfg, artifacts)
    broker = make_broker(cfg, logger)

    def contexto() -> WorkerContext:
        return WorkerContext(cfg=cfg, store=store, artifacts=artifacts, logger=logger)

    consumer = Consumer(cfg, broker, contexto, logger)

    # El latido es lo que hace que este worker APAREZCA en /health y desaparezca
    # solo si se cae. Es telemetria: si Redis no contesta, el worker igual
    # consume. Necesita el mismo cliente que el estado, no una conexion propia.
    latido = None
    client = getattr(store, "client", None)
    if client is not None:
        latido = Heartbeat(client, cfg, colas=consumer.colas, logger=logger).start()

    try:
        consumer.run()
    finally:
        if latido is not None:
            # Un apagado limpio no deja un fantasma 90 s en /health.
            latido.stop()
    return 0


if __name__ == "__main__":                                      # pragma: no cover
    raise SystemExit(main())
