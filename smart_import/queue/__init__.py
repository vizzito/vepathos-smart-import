"""RabbitMQ publisher/consumer para smart-import-tasks (+dlq +delay).

Misma topologia que el optimizador: cola durable, DLQ 24h, delay con TTL+DLX.
El archivo NUNCA viaja por la cola — solo keys de object storage.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

QUEUE_NAME = os.getenv("SMART_IMPORT_QUEUE", "smart-import-tasks")


def _amqp_url() -> str | None:
    url = os.getenv("RABBITMQ_URL") or os.getenv("AMQP_URL")
    if url:
        return url
    host = os.getenv("RABBITMQ_HOST")
    if not host:
        return None
    port = os.getenv("RABBITMQ_PORT", "5672")
    user = os.getenv("RABBITMQ_USER", "guest")
    password = os.getenv("RABBITMQ_PASSWORD", "guest")
    vhost = os.getenv("RABBITMQ_VHOST", "/").lstrip("/") or ""
    return f"amqp://{user}:{password}@{host}:{port}/{vhost}"


class SmartImportQueue:
    """Publica y consume mensajes de smart-import. No-op si no hay broker."""

    def __init__(self, queue_name: str = QUEUE_NAME):
        self.queue_name = queue_name
        self.dlq_name = f"{queue_name}-dlq"
        self.delay_name = f"{queue_name}-delay"
        self._url = _amqp_url()
        self._connection = None
        self._channel = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def _connect(self):
        if not self._url:
            raise RuntimeError("RabbitMQ no configurado (RABBITMQ_URL / RABBITMQ_HOST)")
        try:
            import pika
        except ImportError as exc:
            raise ImportError(
                "pika requerido para RabbitMQ: pip install 'vepathos-smart-import[queue]'"
            ) from exc
        params = pika.URLParameters(self._url)
        params.heartbeat = 60
        self._connection = pika.BlockingConnection(params)
        self._channel = self._connection.channel()
        self._declare(self._channel)
        return self._channel

    def _declare(self, channel) -> None:
        import pika

        channel.queue_declare(
            queue=self.dlq_name,
            durable=True,
            arguments={"x-message-ttl": 86_400_000},  # 24h
        )
        channel.queue_declare(
            queue=self.queue_name,
            durable=True,
            arguments={
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": self.dlq_name,
            },
        )
        # delay: TTL por mensaje + DLX de vuelta a la cola principal
        channel.queue_declare(
            queue=self.delay_name,
            durable=True,
            arguments={
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": self.queue_name,
            },
        )

    def publish(self, message: dict[str, Any], delay_ms: int | None = None) -> bool:
        """Publica. Si Rabbit no esta, retorna False (caller hace sync)."""
        if not self.enabled:
            return False
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        with self._lock:
            try:
                import pika

                ch = self._channel
                if ch is None or self._connection is None or self._connection.is_closed:
                    ch = self._connect()
                props = pika.BasicProperties(
                    delivery_mode=2,
                    content_type="application/json",
                    expiration=str(delay_ms) if delay_ms else None,
                )
                routing = self.delay_name if delay_ms else self.queue_name
                ch.basic_publish(
                    exchange="",
                    routing_key=routing,
                    body=body,
                    properties=props,
                )
                logger.info("publicado %s → %s", message.get("type"), routing)
                return True
            except Exception:
                logger.exception("fallo al publicar en RabbitMQ; el caller puede hacer sync")
                self._channel = None
                self._connection = None
                return False

    def consume(self, handler: Callable[[dict[str, Any]], None], prefetch: int = 1) -> None:
        """Bloquea consumiendo. Ctrl+C para salir."""
        import pika

        while True:
            try:
                ch = self._connect()
                ch.basic_qos(prefetch_count=prefetch)

                def _on_message(channel, method, _properties, body):
                    try:
                        msg = json.loads(body.decode("utf-8"))
                        handler(msg)
                        channel.basic_ack(method.delivery_tag)
                    except Exception:
                        logger.exception("handler fallo; nack → DLQ")
                        channel.basic_nack(method.delivery_tag, requeue=False)

                ch.basic_consume(queue=self.queue_name, on_message_callback=_on_message)
                logger.info("consumiendo %s", self.queue_name)
                ch.start_consuming()
            except KeyboardInterrupt:
                break
            except Exception:
                logger.exception("conexion Rabbit perdida; reintento en 5s")
                self._channel = None
                self._connection = None
                time.sleep(5)


def build_geocode_message(
    job_id: str,
    *,
    input_object_key: str,
    output_object_key: str,
    origin_lat: float | None = None,
    origin_lon: float | None = None,
    depot_city: str | None = None,
    depot_region: str | None = None,
    depot_postcode: str | None = None,
    depot_country: str | None = None,
    depot_address: str | None = None,
    max_distance_km: float | None = None,
    schema: str = "vepathos_flat_v1",
) -> dict[str, Any]:
    return {
        "version": 1,
        "type": "smart_import.geocode",
        "job_id": job_id,
        "input_object_key": input_object_key,
        "output_object_key": output_object_key,
        "schema": schema,
        "options": {
            "origin_lat": origin_lat,
            "origin_lon": origin_lon,
            "depot_city": depot_city,
            "depot_region": depot_region,
            "depot_postcode": depot_postcode,
            "depot_country": depot_country,
            "depot_address": depot_address,
            "max_distance_km": max_distance_km,
        },
    }
