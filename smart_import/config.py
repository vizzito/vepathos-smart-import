"""Configuracion por environment variables.

Regla: TODO lo configurable vive aca y sale de un env var con default razonable.
El servicio arranca sin ningun env seteado, y el mismo binario sirve para local
y para produccion cambiando unicamente el archivo .env.
"""
from __future__ import annotations

import os
import json
from dataclasses import dataclass, fields, field
from pathlib import Path

#: Los tres roles del mismo binario. `embedded` es el default y significa
#: EXACTAMENTE el comportamiento de siempre: estado en memoria, archivos en el
#: disco local y el trabajo pesado adentro del proceso que sirve HTTP. Es el modo
#: de desarrollo, el de la suite de tests y el de un despliegue de un solo nodo:
#: `docker compose up` levanta un Smart Import completo sin infraestructura.
#:
#:   api    → recibe, encola y sirve; no procesa.
#:   worker → consume y procesa; no expone puerto.
ROLES = ("embedded", "api", "worker")

#: Campos que NO se muestran en `/config`. Ese endpoint no pide credenciales
#: (es justamente la forma de verificar que el .env se aplico), asi que
#: cualquier secreto que entre al `Config` se publica a quien alcance el puerto.
SECRET_FIELDS = frozenset({"rabbitmq_password", "redis_password", "worker_token", "api_keys"})


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _float_opt(name: str) -> float | None:
    """`None` cuando la env no esta: el que lee decide como derivar el default."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw.strip() if raw and raw.strip() else default


def parse_csv_list(raw: str | None, default: list[str] | None = None) -> list[str]:
    """Lista CSV (CORS, etc.). Vacio / None → default. Items strippeados."""
    if raw is None or not str(raw).strip():
        return list(default or [])
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _list(name: str, default: list[str]) -> list[str]:
    return parse_csv_list(os.getenv(name), default)


def _role(name: str) -> str:
    """El rol del proceso, validado al leerlo.

    Un typo (`wroker`) no puede degradar en silencio a `embedded`: seria un nodo
    que arranca contento, no consume ninguna cola y nadie nota que falta.
    """
    raw = _str(name, "embedded").lower()
    if raw not in ROLES:
        raise ValueError(f"{name}='{raw}' no es un rol valido; validos: {list(ROLES)}")
    return raw


def _band(primary: str, alias_pct: str, default: float) -> float:
    """Lee banda 0–1. Acepta GEOCODE_*_BAND=0.85 o GEOCODE_*_MIN_PCT=85."""
    for name in (primary, alias_pct):
        raw = os.getenv(name)
        if raw is None or not str(raw).strip():
            continue
        try:
            n = float(str(raw).strip())
        except ValueError:
            continue
        if n > 1.0 and n <= 100.0:
            return n / 100.0
        if 0.0 <= n <= 1.0:
            return n
    return default


def _sibling_route_optimizer_data() -> str:
    """data/ del route-optimizer junto a este repo (layout local de workspace)."""
    sibling = Path(__file__).resolve().parents[2] / "route-optimizer-app" / "data"
    return str(sibling) if sibling.is_dir() else ""


def _resolve_pbf_dir() -> str:
    """PBFs del route-optimizer.

    Prioridad de config:
      1. SMART_IMPORT_PBF_DIR  (explicito)
      2. ROUTE_OPTIMIZER_DATA  (raiz data/: *_tile_* por continente + _extracts)
      3. ../route-optimizer-app/data si existe (dev local)
      4. vacio
    """
    explicit = _str("SMART_IMPORT_PBF_DIR", "")
    if explicit:
        return explicit
    root = _str("ROUTE_OPTIMIZER_DATA", "")
    if root:
        return root
    return _sibling_route_optimizer_data()


@dataclass(frozen=True)
class Config:
    # ---------------- limites de entrada ----------------
    max_file_mb: float = 10.0
    max_rows: int = 50_000

    # ---------------- deteccion de schema ----------------
    auto_accept_threshold: float = 0.90
    review_threshold: float = 0.70
    #: Piso de evidencia para reclamar una columna, POR NIVEL del schema.
    #: Equivocarse no cuesta lo mismo en todos lados: un `address` mal mapeado
    #: manda el camion a otra direccion; un `weight_kg` mal mapeado es un numero
    #: mal en un reporte, que se ve y se corrige. Un solo piso para los tres
    #: obliga a elegir entre perder pesos o arriesgar destinos.
    #: `None` = derivar de `review_threshold` con el margen del nivel.
    mapping_min_delivery: float | None = None
    mapping_min_package: float | None = None
    mapping_min_timewindow: float | None = None
    sample_rows: int = 20
    schema_dir: str = "schemas"
    default_schema: str = "vepathos_flat_v1"

    # ---------------- servicio HTTP ----------------
    host: str = "0.0.0.0"
    port: int = 8100
    api_keys: dict[str, str] = field(default_factory=dict)
    upload_idle_timeout_s: float = 30.0
    cors_origins: tuple[str, ...] = ("*",)
    work_dir: str = "data/jobs"
    verbose: bool = False
    #: Horas que sobrevive un job terminado antes de que se borre su carpeta.
    #: 0 = no barrer (el disco crece sin techo).
    job_ttl_hours: float = 24.0
    #: Cuantos `normalize` pueden correr a la vez. El resto espera y, si no
    #: consigue turno, se va con 429 en vez de degradar a todos.
    #:
    #: El normalize es Python CPU-bound, asi que el GIL lo serializa: medido con
    #: 5k filas, 1 cliente tarda 1.3 s y 8 clientes simultaneos tardan 10.5 s
    #: (8x, cero paralelismo) con la CPU del container clavada en 1 core aunque
    #: tenga 4 asignados. Sin techo, N usuarios no se reparten el servicio: lo
    #: multiplican por N, se comen los 40 hilos del threadpool y el /health
    #: empieza a tardar mas que el timeout del healthcheck.
    max_concurrent_normalize: int = 4
    #: Cuanto espera un request por un turno antes de rendirse con 429. Que no
    #: sea 0 es lo que evita que dos clicks simultaneos se lleven un error.
    normalize_queue_wait_s: float = 20.0
    #: Cuantos imports pueden estar en el sistema a la vez, contando el que
    #: todavia esta subiendo. Es la puerta de admision: se pide ANTES de leer el
    #: body, asi que el rechazo no toca disco.
    #:
    #: Acota el peor caso de una avalancha. Sin esta puerta, 1000 uploads
    #: simultaneos escriben hasta 1000 x `max_file_mb` (10 GB con los defaults)
    #: en el disco que comparte con el cutter antes de que el techo de CPU
    #: rechace a uno solo, porque el archivo se recibe antes de pedir turno.
    max_normalize_queue: int = 32
    #: Techo de tareas concurrentes de uvicorn (conexiones + requests). Por
    #: encima, uvicorn corta con 503 antes de que el request llegue a la app.
    #: Generoso a proposito: los SSE de `/imports/{id}/events` son conexiones
    #: ABIERTAS, y una por usuario mirando su import cuenta para este limite.
    #: 0 = sin techo (el default de uvicorn).
    http_limit_concurrency: int = 512

    # ---------------- extraccion determinística ----------------
    #: region ISO por defecto para telefonos; el job puede sobrescribirla
    default_phone_region: str = ""
    #: un segmento es una entrega a partir de este score
    delivery_accept_threshold: float = 0.55
    #: entre review y accept: probable entrega, pero que la mire un humano
    delivery_review_threshold: float = 0.30
    #: por debajo de esto una direccion NO se acepta ni se manda al geocoder
    address_accept_threshold: float = 0.50
    name_accept_threshold: float = 0.50
    phone_accept_threshold: float = 0.60
    #: TXT: cuanta evidencia estructural hace falta para tratarlo como tabla
    text_tabular_min_consistency: float = 0.80
    text_tabular_min_lines: int = 3
    text_max_prose_ratio: float = 0.20
    text_min_header_score: float = 0.50

    # ---------------- parsing de direcciones ----------------
    #: Solo para benchmarks: heuristic | libpostal | hybrid | enhanced.
    #: En produccion manda `libpostal_enabled`: el heuristico siempre corre y
    #: libpostal es un paso de calidad opcional (ver addresses/enhance.py).
    address_parser: str = "heuristic"
    #: Master switch. false = libpostal no se considera (ni import, ni enhance).
    libpostal_enabled: bool = False

    # ---------------- geocoding (opcional) ----------------
    geocoding_enabled: bool = True
    pbf_dir: str = ""
    index_dir: str = "data/indexes"
    #: Extracts propios (escribible). No es `_extracts` del cutter (:ro).
    extract_dir: str = "data/extracts"
    cache_path: str = "data/cache/geocode_cache.sqlite"
    geocoder_fallback: str = "none"
    match_threshold: float = 0.81
    # Acepta coords desde este score (UI: Review 70–79%, Valid ≥80%).
    low_confidence_threshold: float = 0.70
    # ---- bandas que consume la UI (una sola fuente de verdad) ----
    #: >= esto: verde, la coordenada se usa tal cual (GEOCODE_VALID_BAND / _MIN_PCT).
    #: Tiene que ser == `match_threshold`: `matched` pide
    #: score >= max(match_threshold, valid_band), pero el COLOR lo decide
    #: valid_band sola. Con 0.80 acá y 0.81 arriba, un score de 0.805 volvia con
    #: status=low_confidence y banda VERDE — un pin que le dice al operador
    #: "usalo tal cual" sobre algo que el geocoder marco dudoso.
    geocode_valid_band: float = 0.81
    #: >= esto y < valid: ambar Review (GEOCODE_REVIEW_BAND / _MIN_PCT)
    geocode_review_band: float = 0.70
    #: score crudo minimo para dar pin a un match A NIVEL CALLE (sin altura)
    geocode_street_level_floor: float = 0.60
    #: cuan fuerte tiene que matchear la CALLE para aceptar cualquier coordenada.
    #: Sin esto, coincidir solo en la altura manda el pin a otra calle.
    geocode_street_match_min: float = 0.70
    #: Si True, candidatos que antes eran not_found (calle mala / debajo umbral)
    #: salen como Review CON pin para poder medir distancia en el mapa.
    geocode_soft_reject: bool = True
    #: Score minimo para soft-reject (debajo = not_found duro sin pin). Tiene que
    #: ser >= `geocode_review_band`: por debajo de esa banda, `band_for` pasa la
    #: fila a needs_geocoding y le SACA el pin, asi que el trabajo se hace y se
    #: descarta. Estaba en 0.50 y violaba eso — pero solo cuando alguien
    #: construia `Config()` a mano, porque `from_env` ya usaba 0.70.
    geocode_soft_reject_min: float = 0.70
    # low_confidence mas lejos que esto del depot se trata como not_found
    max_low_confidence_km: float = 15.0
    # hard geofence: cualquier match (matched|low) fuera de este radio = not_found
    max_geocode_distance_km: float = 500.0
    geocode_workers: int = 1
    autobuild_index: bool = True      # construir el indice si falta
    autoextract: bool = True          # cortar extract de ciudad si no hay
    extract_margin_km: float = 15.0
    extract_round_deg: float = 0.1
    extract_max_km: float = 80.0
    osmium_bin: str = ""

    # ---------------- reparto entre nodos ----------------
    #: Que hace este proceso. Ver `ROLES`. Todo lo de esta seccion tiene default
    #: inerte: con `embedded` nada de esto se lee, y el servicio no necesita ni
    #: broker ni Redis para arrancar.
    role: str = "embedded"

    #: Broker de tareas. Mismos nombres de env que usa el optimizer, para que la
    #: operacion sea una sola. Host vacio = no hay cola (modo embebido).
    rabbitmq_host: str = ""
    rabbitmq_port: int = 5672
    rabbitmq_user: str = ""
    rabbitmq_password: str = ""
    rabbitmq_vhost: str = "/"
    queue_prefix: str = "smart-import"

    #: Estado compartido de los jobs. DB propia: el optimizer corre todo en la 0
    #: y mezclar keyspaces hace que un `FLUSHDB` de uno se lleve al otro.
    redis_host: str = ""
    redis_port: int = 6379
    redis_password: str = ""
    redis_db: int = 1
    redis_prefix: str = "smartimport:"

    #: Que colas consume este worker. Un nodo sin PBF NO se suscribe a geocode,
    #: asi que no puede recibir una tarea que no sabe hacer: no hay rebote.
    consume_normalize: bool = True
    consume_geocode: bool = False
    #: Tareas en paralelo por worker. Es el mismo techo que hoy impone
    #: `max_concurrent_normalize` en la API, pero repartido y sin 429.
    worker_slots: int = 2
    max_requeue_attempts: int = 10
    shutdown_drain_s: float = 60.0
    task_timeout_normalize_s: float = 300.0
    task_timeout_geocode_s: float = 1800.0
    node_heartbeat_ttl_s: float = 90.0
    #: Heartbeat AMQP, en segundos. pika manda trafico cada mitad de esto.
    #:
    #: 30 y no el default de pika (600) porque un worker remoto habla con el
    #: broker por NAT o por un tunel SSH, y esos cortan las conexiones ociosas
    #: entre 5 y 15 minutos. Con 600, pika manda algo cada 300 s —pegado a ese
    #: borde— y cuando la entrada se borra el consumidor queda "conectado"
    #: sobre un socket muerto: la cola crece y no hay un solo error en el log.
    #:
    #: El valor largo que traiamos venia de suponer que un heartbeat corto se
    #: pierde si una tarea no cede el thread. Aca no pasa: las tareas corren en
    #: un pool y el IO loop queda libre en el thread principal.
    #:
    #: Mismo nombre de variable que el optimizer, que aprendio esto antes.
    rabbitmq_heartbeat_s: int = 30
    #: Cuanto se espera antes de dar por muerto a un nodo que dejo de dar
    #: senales, y por lo tanto cuanto tarda otro en retomar su trabajo. NO es
    #: cuanto puede durar una tarea: mientras el nodo vive, renueva el lock
    #: cada `ttl/3`. Bajarlo acelera el failover; demasiado bajo hace que una
    #: pausa larga se confunda con una muerte y el archivo se haga dos veces.
    #: El consumer le aplica un piso de 10 s.
    run_lock_ttl_s: float = 30.0
    #: Cuantas tareas pueden estar esperando en la cola antes de que la api
    #: empiece a rechazar imports nuevos con 429.
    #:
    #: Repone la contrapresion que se perdio al mandar el trabajo a la cola. Con
    #: todo en proceso, un servicio saturado contestaba "reintenta en 20 s" y el
    #: operador lo veia. Encolando, la misma sobrecarga se acepta y se convierte
    #: en latencia: nadie recibe un error, los imports simplemente tardan cada
    #: vez mas y no hay ninguna senal de que el sistema esta al limite.
    #:
    #: 0 = sin techo (aceptar siempre). Se mide contra la foto de la flota, que
    #: se refresca cada FLEET_SCAN_TTL_S: es una valvula gruesa, no un limitador
    #: exacto, y no cuesta una llamada de red por request.
    max_queue_depth: int = 500
    #: Techo de un artefacto que un worker devuelve por `/internal`.
    #:
    #: La puerta publica ya acota lo que ENTRA (`max_file_mb`), pero lo que sale
    #: del pipeline puede ser mucho mas grande que su entrada: 50k filas con
    #: `diagnostics=true` son varias veces el .xlsx original. Sin techo, un
    #: worker con un bug escribe hasta llenar el disco de la api, que es el
    #: mismo disco donde vive routehub.
    #:
    #: Generoso a proposito: rechazar un resultado legitimo obliga a rehacer
    #: todo el trabajo. 0 = sin techo.
    max_artifact_mb: float = 100.0

    #: De donde baja el worker los archivos del job y adonde devuelve el
    #: resultado. Es el rol api, alcanzado por HTTP saliente con token.
    api_url: str = ""
    worker_token: str = ""
    #: Espacio de trabajo del worker. Se borra SIEMPRE al terminar la tarea
    #: (exito, error o timeout): un worker no acumula nada.
    scratch_dir: str = ""

    #: Cuanto espera `POST /imports` a que el resultado este listo antes de
    #: responder 202 con el job_id. Preserva el 201 sincrono de hoy para el caso
    #: rapido, que es el que el webclient consume del cuerpo. 0 = siempre 202.
    default_wait_s: float = 30.0

    #: Cuanto se corre el piso segun el costo de equivocarse en ese nivel.
    #: Es relativo a `review_threshold` a proposito: mover el knob global sigue
    #: moviendo los tres, y la escalera entre ellos no cambia.
    LEVEL_MARGIN = {"delivery": 0.0, "timewindow": -0.05, "package": -0.10}

    def mapping_floor(self, level: str) -> float:
        """Evidencia minima para asignar una columna a un campo de ese nivel."""
        explicit = {
            "delivery": self.mapping_min_delivery,
            "package": self.mapping_min_package,
            "timewindow": self.mapping_min_timewindow,
        }.get(level)
        if explicit is not None:
            return float(explicit)
        return max(0.0, self.review_threshold + self.LEVEL_MARGIN.get(level, 0.0))

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            max_file_mb=_float("SMART_IMPORT_MAX_FILE_MB", 10.0),
            max_rows=_int("SMART_IMPORT_MAX_ROWS", 50_000),

            auto_accept_threshold=_float("AUTO_ACCEPT_THRESHOLD", 0.90),
            review_threshold=_float("REVIEW_THRESHOLD", 0.70),
            mapping_min_delivery=_float_opt("MAPPING_MIN_DELIVERY"),
            mapping_min_package=_float_opt("MAPPING_MIN_PACKAGE"),
            mapping_min_timewindow=_float_opt("MAPPING_MIN_TIMEWINDOW"),
            sample_rows=_int("SMART_IMPORT_SAMPLE_ROWS", 20),
            schema_dir=_str("SMART_IMPORT_SCHEMA_DIR", "schemas"),
            default_schema=_str("SMART_IMPORT_DEFAULT_SCHEMA", "vepathos_flat_v1"),

            host=_str("SMART_IMPORT_HOST", "0.0.0.0"),
            port=_int("SMART_IMPORT_PORT", 8100),
            api_keys=_api_keys(),
            upload_idle_timeout_s=_float("SMART_IMPORT_UPLOAD_IDLE_TIMEOUT_S", 30.0),
            cors_origins=tuple(_list("SMART_IMPORT_CORS_ORIGINS", ["*"])),
            work_dir=_str("SMART_IMPORT_WORK_DIR", "data/jobs"),
            verbose=_bool("SMART_IMPORT_VERBOSE", False),
            job_ttl_hours=_float("SMART_IMPORT_JOB_TTL_HOURS", 24.0),
            max_concurrent_normalize=_int("SMART_IMPORT_MAX_CONCURRENT_NORMALIZE", 4),
            normalize_queue_wait_s=_float("SMART_IMPORT_NORMALIZE_QUEUE_WAIT_S", 20.0),
            max_normalize_queue=_int("SMART_IMPORT_MAX_NORMALIZE_QUEUE", 32),
            http_limit_concurrency=_int("SMART_IMPORT_HTTP_LIMIT_CONCURRENCY", 512),

            default_phone_region=_str("SMART_IMPORT_DEFAULT_PHONE_REGION", ""),
            delivery_accept_threshold=_float("SMART_IMPORT_DELIVERY_ACCEPT_THRESHOLD", 0.55),
            delivery_review_threshold=_float("SMART_IMPORT_DELIVERY_REVIEW_THRESHOLD", 0.30),
            address_accept_threshold=_float("SMART_IMPORT_ADDRESS_ACCEPT_THRESHOLD", 0.50),
            name_accept_threshold=_float("SMART_IMPORT_NAME_ACCEPT_THRESHOLD", 0.50),
            phone_accept_threshold=_float("SMART_IMPORT_PHONE_ACCEPT_THRESHOLD", 0.60),
            text_tabular_min_consistency=_float("SMART_IMPORT_TEXT_MIN_CONSISTENCY", 0.80),
            text_tabular_min_lines=_int("SMART_IMPORT_TEXT_MIN_LINES", 3),
            text_max_prose_ratio=_float("SMART_IMPORT_TEXT_MAX_PROSE_RATIO", 0.20),
            text_min_header_score=_float("SMART_IMPORT_TEXT_MIN_HEADER_SCORE", 0.50),

            address_parser=_str("SMART_IMPORT_ADDRESS_PARSER", "heuristic"),
            libpostal_enabled=_bool("SMART_IMPORT_LIBPOSTAL_ENABLED", False),


            geocoding_enabled=_bool("SMART_IMPORT_GEOCODING_ENABLED", True),
            pbf_dir=_resolve_pbf_dir(),
            index_dir=_str("SMART_IMPORT_INDEX_DIR", "data/indexes"),
            extract_dir=_str("SMART_IMPORT_EXTRACT_DIR", "data/extracts"),
            cache_path=_str("SMART_IMPORT_CACHE_PATH", "data/cache/geocode_cache.sqlite"),
            geocoder_fallback=_str("GEOCODER_FALLBACK", "none"),
            match_threshold=_float("GEOCODE_MATCH_THRESHOLD", 0.81),
            low_confidence_threshold=_float("GEOCODE_LOW_CONFIDENCE_THRESHOLD", 0.70),
            # UI bands (0–1 o 0–100). Fuente unica — la web NO redefine cortes.
            geocode_valid_band=_band("GEOCODE_VALID_BAND", "GEOCODE_VALID_MIN_PCT", 0.81),
            geocode_review_band=_band("GEOCODE_REVIEW_BAND", "GEOCODE_REVIEW_MIN_PCT", 0.70),
            geocode_street_level_floor=_float("GEOCODE_STREET_LEVEL_FLOOR", 0.60),
            geocode_street_match_min=_float("GEOCODE_STREET_MATCH_MIN", 0.70),
            geocode_soft_reject=_bool("GEOCODE_SOFT_REJECT", True),
            geocode_soft_reject_min=_float("GEOCODE_SOFT_REJECT_MIN", 0.70),
            max_low_confidence_km=_float("GEOCODE_MAX_LOW_CONFIDENCE_KM", 15.0),
            max_geocode_distance_km=_float("GEOCODE_MAX_DISTANCE_KM", 500.0),
            geocode_workers=_int("SMART_IMPORT_GEOCODE_WORKERS", 1),
            autobuild_index=_bool("SMART_IMPORT_AUTOBUILD_INDEX", True),
            autoextract=_bool("SMART_IMPORT_AUTOEXTRACT", True),
            extract_margin_km=_float("SMART_IMPORT_EXTRACT_MARGIN_KM", 15.0),
            extract_round_deg=_float("SMART_IMPORT_EXTRACT_ROUND_DEG", 0.1),
            extract_max_km=_float("SMART_IMPORT_EXTRACT_MAX_KM", 80.0),
            osmium_bin=_str("OSMIUM_BIN", ""),

            role=_role("SMART_IMPORT_ROLE"),
            rabbitmq_host=_str("RABBITMQ_HOST", ""),
            rabbitmq_port=_int("RABBITMQ_PORT", 5672),
            rabbitmq_user=_str("RABBITMQ_USER", ""),
            rabbitmq_password=_str("RABBITMQ_PASSWORD", ""),
            rabbitmq_vhost=_str("RABBITMQ_VHOST", "/"),
            queue_prefix=_str("SMART_IMPORT_QUEUE_PREFIX", "smart-import"),
            redis_host=_str("REDIS_HOST", ""),
            redis_port=_int("REDIS_PORT", 6379),
            redis_password=_str("REDIS_PASSWORD", ""),
            redis_db=_int("SMART_IMPORT_REDIS_DB", 1),
            redis_prefix=_str("SMART_IMPORT_REDIS_PREFIX", "smartimport:"),
            consume_normalize=_bool("SMART_IMPORT_CONSUME_NORMALIZE", True),
            consume_geocode=_bool("SMART_IMPORT_CONSUME_GEOCODE", False),
            worker_slots=_int("SMART_IMPORT_WORKER_SLOTS", 2),
            max_queue_depth=_int("SMART_IMPORT_MAX_QUEUE_DEPTH", 500),
            max_artifact_mb=_float("SMART_IMPORT_MAX_ARTIFACT_MB", 100.0),
            max_requeue_attempts=_int("SMART_IMPORT_MAX_REQUEUE_ATTEMPTS", 10),
            shutdown_drain_s=_float("SMART_IMPORT_SHUTDOWN_DRAIN_S", 60.0),
            task_timeout_normalize_s=_float("SMART_IMPORT_TASK_TIMEOUT_NORMALIZE_S", 300.0),
            task_timeout_geocode_s=_float("SMART_IMPORT_TASK_TIMEOUT_GEOCODE_S", 1800.0),
            run_lock_ttl_s=_float("SMART_IMPORT_RUN_LOCK_TTL_S", 30.0),
            node_heartbeat_ttl_s=_float("SMART_IMPORT_NODE_HEARTBEAT_TTL_S", 90.0),
            rabbitmq_heartbeat_s=_int("RABBITMQ_HEARTBEAT", 30),
            api_url=_str("SMART_IMPORT_API_URL", ""),
            worker_token=_str("SMART_IMPORT_WORKER_TOKEN", ""),
            scratch_dir=_str("SMART_IMPORT_SCRATCH_DIR", ""),
            default_wait_s=_float("SMART_IMPORT_DEFAULT_WAIT_S", 30.0),
        )

    def replace(self, **changes) -> "Config":
        current = {f.name: getattr(self, f.name) for f in fields(self)}
        current.update(changes)
        return Config(**current)

    def describe(self) -> dict:
        """Config efectiva, para /config y para el log de arranque.

        Los secretos salen como `"***"` si estan puestos y como `""` si no: se
        sigue pudiendo verificar que el .env se aplico sin publicar el valor.
        """
        return {f.name: ("***" if (f.name in SECRET_FIELDS and getattr(self, f.name))
                         else getattr(self, f.name))
                for f in fields(self)}


def _api_keys() -> dict[str, str]:
    raw = os.getenv("SMART_IMPORT_API_KEYS", "")
    if not raw:
        return {}
    try:
        keys = json.loads(raw)
        if not isinstance(keys, dict) or not keys or any(
            not isinstance(k, str) or len(k) < 32 or not k.isascii()
            or not isinstance(v, str) or not v.strip() for k, v in keys.items()
        ):
            raise ValueError
        return keys
    except (ValueError, TypeError):
        raise ValueError("SMART_IMPORT_API_KEYS debe mapear tokens ASCII de 32+ caracteres a tenants") from None
