"""Carga perezosa del modelo de extraccion, UNA sola vez por proceso.

En produccion el proceso vive esperando trabajo: el costo de carga se paga al
arrancar el contenedor, no en cada archivo. Por eso el benchmark mide carga e
inferencia por separado.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

_LOCK = threading.Lock()
_CACHE: dict[str, "LoadedModel"] = {}
_THREADS_CONFIGURED = False


def cpu_quota() -> int:
    """CPUs que el cgroup permite de verdad, no las que ve `os.cpu_count()`.

    Dentro de un container, `os.cpu_count()` devuelve las del HOST. Torch lanza
    esa cantidad de hilos OpenMP y despues el cgroup lo limita a la cuota real:
    14 hilos peleandose por 2 CPUs de cuota. Medido: 33.2 s por fila con 14
    hilos contra 11.98 s con 2, mismo limite. La contencion cuesta casi 3x.
    """
    try:                                            # cgroup v2
        raw = open("/sys/fs/cgroup/cpu.max").read().split()
        if raw[0] != "max":
            return max(1, round(int(raw[0]) / int(raw[1])))
    except OSError:
        pass
    try:                                            # cgroup v1
        quota = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        period = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        if quota > 0:
            return max(1, round(quota / period))
    except OSError:
        pass
    return os.cpu_count() or 1


def configure_threads() -> int:
    """Alinea los hilos de torch con la cuota real. ANTES de importar torch.

    OMP_NUM_THREADS tiene que estar seteado antes de que OpenMP se inicialice:
    llamar a `torch.set_num_threads()` despues no deshace la contencion.
    Respeta el valor si el operador ya lo fijo.
    """
    global _THREADS_CONFIGURED
    if _THREADS_CONFIGURED:
        return int(os.environ.get("OMP_NUM_THREADS", 1))

    hilos = int(os.environ["OMP_NUM_THREADS"]) if os.environ.get("OMP_NUM_THREADS") \
        else cpu_quota()
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ.setdefault(var, str(hilos))
    _THREADS_CONFIGURED = True
    return hilos


@dataclass
class LoadedModel:
    name: str
    device: str
    tokenizer: object
    model: object
    load_seconds: float
    threads: int = 0


class ModelUnavailable(RuntimeError):
    """No se pudo cargar el modelo. NUNCA debe romper un import: se cae a reglas."""


def resolve_device(requested: str) -> str:
    if requested and requested != "auto":
        return requested
    configure_threads()
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
            hilos = configure_threads()
            tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
            model = AutoModelForCausalLM.from_pretrained(
                model_name, trust_remote_code=True, torch_dtype="auto")
            model.eval()
            if resolved != "cpu":
                model = model.to(resolved)
        except Exception as exc:
            raise ModelUnavailable(f"no se pudo cargar {model_name}: {exc}") from exc

        try:
            import torch
            torch.set_num_threads(hilos)            # por si OpenMP ya arranco
        except Exception:
            pass

        loaded = LoadedModel(name=model_name, device=resolved, tokenizer=tokenizer,
                             model=model, load_seconds=time.perf_counter() - started,
                             threads=hilos)
        _CACHE[key] = loaded
        return loaded


def is_loaded(model_name: str, device: str = "cpu") -> bool:
    return f"{model_name}@{device}" in _CACHE
