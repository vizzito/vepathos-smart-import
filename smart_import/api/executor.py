"""Bound both running and queued embedded geocode jobs."""
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore


class QueueFull(RuntimeError):
    pass


class BoundedExecutor(ThreadPoolExecutor):
    def __init__(self, max_workers, max_pending, **kwargs):
        super().__init__(max_workers=max_workers, **kwargs)
        self._capacity = BoundedSemaphore(max(max_workers, max_pending))

    def submit(self, fn, /, *args, **kwargs):
        if not self._capacity.acquire(blocking=False):
            raise QueueFull("Geocode queue full")
        try:
            future = super().submit(fn, *args, **kwargs)
        except BaseException:
            self._capacity.release()
            raise
        future.add_done_callback(lambda _: self._capacity.release())
        return future
