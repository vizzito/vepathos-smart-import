"""Un broker en memoria, para probar lo que decide el worker sin RabbitMQ.

La regla de la suite es que ninguna fase agregue infraestructura a CI, y lo que
hay que probar del consumidor no es pika: es la POLITICA — que se reintenta, con
cuanta demora, que se aparta, que se confirma y que se devuelve al bajar. Todo
eso vive del lado nuestro del contrato.

Guarda todo lo que pasa (`publicadas`, `dlq`, `ackeadas`, `nackeadas`) para que
un test pregunte por el efecto y no por la implementacion.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from smart_import.queue.broker import Broker, Delivery
from smart_import.queue.envelope import Task


@dataclass
class Publicada:
    task: Task
    delay_s: float


class FakeBroker(Broker):
    def __init__(self) -> None:
        self.publicadas: list[Publicada] = []
        self.dlq: list[tuple[Task, str]] = []
        self.ackeadas: list[str] = []
        self.nackeadas: list[tuple[str, bool]] = []
        self.declarada = False
        self.cerrado = False
        self._pendientes: list[Delivery] = []
        self._parar = threading.Event()
        self._tag = 0

    # ---------------------------------------------------------- contrato

    def declare(self) -> None:
        self.declarada = True

    def publish(self, task: Task, *, delay_s: float = 0.0) -> None:
        self.publicadas.append(Publicada(task, delay_s))

    def send_to_dlq(self, task: Task, motivo: str) -> None:
        self.dlq.append((task, motivo))

    def depths(self) -> dict[str, int]:
        """Lo que este broker sabe de verdad: lo publicado y todavia no ackeado.

        El fake puede contar exacto, asi que los tests de contrapresion no
        dependen de simular a RabbitMQ: preguntan lo mismo que preguntaria la
        api en produccion.
        """
        colas: dict[str, int] = {"smart-import-dlq": len(self.dlq)}
        for publicada in self.publicadas:
            tipo = publicada.task.type
            cola = f"smart-import-{tipo}" if publicada.delay_s <= 0 else f"smart-import-{tipo}-delay"
            colas[cola] = colas.get(cola, 0) + 1
        return colas

    def consume(self, colas, prefetch, on_message) -> None:
        self.colas = list(colas)
        self.prefetch = prefetch
        while not self._parar.is_set() and self._pendientes:
            on_message(self._pendientes.pop(0))

    def ack(self, receipt: str) -> None:
        self.ackeadas.append(receipt)

    def nack(self, receipt: str, *, requeue: bool) -> None:
        self.nackeadas.append((receipt, requeue))

    def stop(self) -> None:
        self._parar.set()

    def close(self) -> None:
        self.cerrado = True

    # ---------------------------------------------------------- ayudas

    def entregar(self, task: Task, cola: str = "smart-import-normalize") -> Delivery:
        """Un mensaje entregado, como lo pasaria pika al callback."""
        self._tag += 1
        return Delivery(body=task.as_bytes(), receipt=f"1:{self._tag}", queue=cola)

    def entregar_crudo(self, cuerpo: bytes,
                       cola: str = "smart-import-normalize") -> Delivery:
        self._tag += 1
        return Delivery(body=cuerpo, receipt=f"1:{self._tag}", queue=cola)

    @property
    def reintentos(self) -> list[Task]:
        return [p.task for p in self.publicadas]


class PoolSincrono:
    """Ejecuta en el acto. Los tests no tienen que esperar a un thread."""

    def __init__(self) -> None:
        self.enviadas = 0

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future

        self.enviadas += 1
        futuro: Future = Future()
        try:
            futuro.set_result(fn(*args, **kwargs))
        except BaseException as exc:            # pragma: no cover
            futuro.set_exception(exc)
        return futuro

    def shutdown(self, wait: bool = True) -> None:
        pass
