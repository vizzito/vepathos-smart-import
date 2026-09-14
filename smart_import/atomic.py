"""Publish complete files with rename, cleaning abandoned writes on failure."""
from contextlib import contextmanager
from pathlib import Path
import os
import uuid


@contextmanager
def output_path(destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.stem + "." + uuid.uuid4().hex + ".partial" + destination.suffix)
    try:
        yield temp
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
