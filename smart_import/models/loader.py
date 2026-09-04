"""Carga perezosa del modelo de extraccion, UNA sola vez por proceso.

En produccion el proceso vive esperando trabajo: el costo de carga se paga al
arrancar el contenedor, no en cada archivo. Por eso el benchmark mide carga e
inferencia por separado.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

_LOCK = threading.Lock()
_CACHE: dict[str, "LoadedModel"] = {}


@dataclass
class LoadedModel:
    name: str
    device: str
    tokenizer: object
    model: object
    load_seconds: float


class ModelUnavailable(RuntimeError):
    """No se pudo cargar el modelo. NUNCA debe romper un import: se cae a reglas."""


def resolve_device(requested: str) -> str:
    if requested and requested != "auto":
        return requested
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def load(model_name: str, device: str = "cpu") -> LoadedModel:
    key = f"{model_name}@{device}"
    if key in _CACHE:
        return _CACHE[key]

    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise ModelUnavailable(
                "faltan transformers/torch. Instalalos con: pip install -e '.[ai]'"
            ) from exc

        started = time.perf_counter()
        try:
            resolved = resolve_device(device)
            tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
            model = AutoModelForCausalLM.from_pretrained(
                model_name, trust_remote_code=True, torch_dtype="auto")
            model.eval()
            if resolved != "cpu":
                model = model.to(resolved)
        except Exception as exc:
            raise ModelUnavailable(f"no se pudo cargar {model_name}: {exc}") from exc

        loaded = LoadedModel(name=model_name, device=resolved, tokenizer=tokenizer,
                             model=model, load_seconds=time.perf_counter() - started)
        _CACHE[key] = loaded
        return loaded


def is_loaded(model_name: str, device: str = "cpu") -> bool:
    return f"{model_name}@{device}" in _CACHE
