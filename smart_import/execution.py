"""Per-attempt identity and cancellation, shared by stores and artifact writers."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading


class ExecutionLost(RuntimeError):
    """A stale or cancelled attempt must never publish state or artifacts."""


@dataclass
class Execution:
    job_id: str
    token: str
    cancelled: threading.Event = field(default_factory=threading.Event)


current: ContextVar[Execution | None] = ContextVar("smart_import_execution", default=None)


def token_for(job_id: str) -> str | None:
    execution = current.get()
    if execution is None or execution.job_id != job_id:
        return None
    if execution.cancelled.is_set():
        raise ExecutionLost("execution cancelled or lease lost")
    return execution.token


@contextmanager
def executing(execution):
    reset = current.set(execution)
    try:
        yield
    finally:
        current.reset(reset)
