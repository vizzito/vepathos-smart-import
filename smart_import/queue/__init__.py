"""La cola de tareas: quien publica, quien consume y que viaja.

En modo `embedded` —el default— no hay cola: `make_broker` devuelve `None` y la
API hace el trabajo en su propio proceso, como siempre. La cola aparece recien
cuando el despliegue se parte en roles.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .broker import Broker, Delivery, QueueNames, RabbitBroker
from .consumer import Consumer
from .envelope import (
    GEOCODE, NORMALIZE, TYPES, InvalidTask, Task, geocode_task, normalize_task,
)

if TYPE_CHECKING:
    from ..config import Config

__all__ = [
    "Broker", "RabbitBroker", "Delivery", "QueueNames", "Consumer",
    "Task", "InvalidTask", "NORMALIZE", "GEOCODE", "TYPES",
    "normalize_task", "geocode_task", "make_broker",
]


def make_broker(cfg: "Config", logger=None) -> Broker | None:
    """El transporte de tareas de este proceso, o `None` si no hay reparto.

    Sin broker configurado en un rol que lo necesita se falla al arrancar y no
    en el primer upload: una API que acepta archivos y no tiene a quien
    encargarle el trabajo deja jobs colgados que nadie va a terminar nunca.
    """
    if cfg.role == "embedded":
        return None
    if not cfg.rabbitmq_host:
        raise ValueError(
            f"SMART_IMPORT_ROLE={cfg.role} reparte el trabajo por una cola y "
            "RABBITMQ_HOST esta vacio. Configuralo, o volve a "
            "SMART_IMPORT_ROLE=embedded.")
    if cfg.role == "worker":
        return RabbitBroker(cfg, logger)
    # La API publica con alguien esperando la respuesta: si el broker no esta,
    # conviene decirlo en segundos y no despues de un minuto de reintentos.
    return RabbitBroker(cfg, logger, intentos_conexion=1, reintentos_publicacion=2)
