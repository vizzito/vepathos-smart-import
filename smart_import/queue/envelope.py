"""Lo unico que viaja por la cola: quien tiene que hacer que, y sobre que job.

Un mensaje NO lleva datos del usuario. Lleva el `job_id` y los parametros de la
peticion; los archivos los busca el worker con el `ArtifactStore` y el estado lo
lee del store compartido. Es a proposito, y por tres razones concretas:

  - un broker no es un almacen: un CSV de 10 MB por mensaje llena la memoria de
    RabbitMQ, que es la pieza que menos puede fallar de las tres;
  - el mensaje se redeliverea; los datos no. Si el payload viajara, un reintento
    trabajaria sobre una copia vieja del archivo;
  - se puede leer una cola atascada sin ver datos de nadie.

Los parametros son JSON puro (`origin_lat`, `depot_city`, …), no objetos de
dominio ya construidos: el `DepotContext` se arma en el worker con la misma
funcion que usa la API hoy. Serializar objetos ata el formato del mensaje a la
forma interna de una clase, y eso se rompe en el primer deploy escalonado, con
mensajes viejos en vuelo y codigo nuevo consumiendo.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

#: Los dos trabajos pesados. Son los mismos dos que hoy corren en el proceso de
#: la API: normalizar un archivo y geolocalizar sus filas sin coordenadas.
NORMALIZE = "normalize"
GEOCODE = "geocode"
TYPES = (NORMALIZE, GEOCODE)

#: Tope del mensaje serializado. No es una cota de recursos —64 KB no lastiman a
#: nadie— sino un alambre-trampa: si alguien empieza a mandar el contenido del
#: archivo por la cola, revienta aca y no en produccion a las tres semanas.
MAX_BYTES = 64 * 1024


class InvalidTask(ValueError):
    """El cuerpo del mensaje no es una tarea que este servicio sepa hacer.

    Es permanente por definicion: reintentarlo da exactamente lo mismo, asi que
    el consumidor lo manda a la DLQ sin reencolar. Un mensaje asi es un bug de
    quien publica o basura de otro sistema apuntando a nuestra cola.
    """


@dataclass(frozen=True)
class Task:
    """Una unidad de trabajo. Inmutable: un reintento es una tarea nueva."""

    type: str
    job_id: str
    params: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: Cuantas veces se intento, contando esta. Viaja en el mensaje y no en un
    #: contador aparte para que se pueda leer un mensaje trabado y saber cuanto
    #: lleva sufriendo, sin cruzar nada con Redis.
    attempt: int = 1
    created_at: float = field(default_factory=time.time)
    #: Por que fracaso. Solo lo lleva el que termino en la DLQ, para que quien
    #: la mire sepa que paso sin tener que buscar el log del nodo que murio.
    error: str | None = None

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            raise InvalidTask(f"tipo de tarea '{self.type}' desconocido; "
                              f"validos: {sorted(TYPES)}")
        if not self.job_id or not isinstance(self.job_id, str):
            raise InvalidTask("la tarea no dice sobre que job trabajar")
        if not isinstance(self.params, dict):
            raise InvalidTask("los parametros de la tarea tienen que ser un objeto")
        if not isinstance(self.attempt, int) or self.attempt < 1:
            raise InvalidTask(f"intento invalido: {self.attempt!r}")

    def as_bytes(self) -> bytes:
        cuerpo = json.dumps(asdict(self), ensure_ascii=False).encode("utf-8")
        if len(cuerpo) > MAX_BYTES:
            raise InvalidTask(
                f"la tarea pesa {len(cuerpo)} bytes y el tope son {MAX_BYTES}: "
                "por la cola viajan referencias, no archivos")
        return cuerpo

    @classmethod
    def from_bytes(cls, cuerpo: bytes) -> "Task":
        """Reconstruye una tarea recibida. Todo lo que no entienda es `InvalidTask`.

        Tolera claves de mas —las escribio una version mas nueva durante un
        deploy escalonado— y usa los defaults para las que falten.
        """
        if len(cuerpo) > MAX_BYTES:
            raise InvalidTask(f"mensaje de {len(cuerpo)} bytes; el tope son {MAX_BYTES}")
        try:
            crudo = json.loads(cuerpo)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InvalidTask(f"el cuerpo no es JSON: {exc}") from exc
        if not isinstance(crudo, dict):
            raise InvalidTask(f"el cuerpo es {type(crudo).__name__}, no un objeto")

        conocidas = {"type", "job_id", "params", "task_id", "attempt", "created_at",
                     "error"}
        try:
            return cls(**{k: v for k, v in crudo.items() if k in conocidas})
        except TypeError as exc:
            # Falta algo sin lo que la tarea no significa nada (el tipo, el job).
            raise InvalidTask(f"al mensaje le falta lo esencial: {exc}") from exc

    def reintento(self, error: str) -> "Task":
        """La misma tarea, un intento mas arriba."""
        from dataclasses import replace
        return replace(self, attempt=self.attempt + 1, error=error)

    def __str__(self) -> str:
        return f"{self.type}/{self.job_id} (intento {self.attempt})"


def normalize_task(job_id: str, **params: Any) -> Task:
    return Task(type=NORMALIZE, job_id=job_id, params=params)


def geocode_task(job_id: str, **params: Any) -> Task:
    return Task(type=GEOCODE, job_id=job_id, params=params)
