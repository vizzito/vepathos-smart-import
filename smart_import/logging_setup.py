"""Logging legible por humanos, con las etapas del pipeline visibles.

El objetivo es que mirando la consola se entienda QUE esta haciendo el servicio
y POR QUE tomo cada decision, sin tener que abrir el report despues.
"""
from __future__ import annotations

import logging
import os
import sys
import time

LOGGER_NAME = "smart_import"

# etapa -> color ANSI (solo si la salida es una terminal)
_COLORS = {
    "READ": "\033[36m", "DETECT": "\033[35m", "NORMALIZE": "\033[34m",
    "ASSEMBLE": "\033[34m", "EMIT": "\033[32m", "INDEX": "\033[33m",
    "GEOCODE": "\033[33m", "EXTRACT": "\033[35m", "ADDRESS": "\033[35m",
    "HTTP": "\033[36m",
    "WARN": "\033[31m", "DONE": "\033[32m",
}
_RESET = "\033[0m"
_DIM = "\033[2m"


class StageFormatter(logging.Formatter):
    def __init__(self, color: bool):
        super().__init__()
        self.color = color
        self.start = time.perf_counter()

    def format(self, record: logging.LogRecord) -> str:
        stage = getattr(record, "stage", "")
        elapsed = time.perf_counter() - self.start
        message = record.getMessage()

        if record.levelno >= logging.WARNING and not stage:
            stage = "WARN"
        label = f"{stage:<9}" if stage else " " * 9

        if self.color:
            tint = _COLORS.get(stage, "")
            label = f"{tint}{label}{_RESET}" if tint else label
            timestamp = f"{_DIM}{elapsed:7.3f}s{_RESET}"
        else:
            # Fuera de una terminal esto va a un agregador de logs, donde
            # "12.480s desde que arranco el proceso" no sirve para correlacionar
            # un incidente con nada. En la terminal el elapsed sigue siendo lo
            # comodo para leer una corrida.
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                                      time.localtime(record.created))

        line = f"  {timestamp}  {label}  {message}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup(verbose: bool = False, quiet: bool = False) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False

    level = logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO)
    logger.setLevel(level)

    handler = logging.StreamHandler(sys.stderr)
    use_color = sys.stderr.isatty() and not os.getenv("NO_COLOR")
    handler.setFormatter(StageFormatter(color=use_color))
    logger.addHandler(handler)
    return logger


def get_logger(suffix: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)


def stage(logger: logging.Logger, name: str, message: str,
          level: int = logging.INFO, **fields) -> None:
    """Una linea de etapa. Los campos extra salen como `clave=valor`."""
    if fields:
        rendered = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
        message = f"{message} {rendered}" if message else rendered
    logger.log(level, message, extra={"stage": name})


def detail(logger: logging.Logger, message: str) -> None:
    """Sub-linea indentada, para enumerar decisiones dentro de una etapa.

    Va en DEBUG a proposito: estas lineas llevan direcciones y nombres de
    clientes finales (el detalle por fila del geocode, las muestras de lo que se
    ignoro). En INFO terminaban en el json-file de Docker, sin rotacion, para
    siempre. Con SMART_IMPORT_VERBOSE=true vuelven a verse — que es justamente
    para lo que existe el flag.
    """
    logger.debug(f"    {message}", extra={"stage": ""})
