"""Object storage adapters — local disk (default) o S3/MinIO.

Misma interfaz que usara el consumer de RabbitMQ: put/get/delete por key.
En local sin MinIO, LocalStorage escribe bajo SMART_IMPORT_WORK_DIR.
"""
from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse


class ObjectStorage(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes | BinaryIO, content_type: str = "application/octet-stream") -> str:
        """Guarda y devuelve la key canonica."""

    @abstractmethod
    def get(self, key: str) -> bytes:
        ...

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        ...

    @abstractmethod
    def local_path(self, key: str) -> Path | None:
        """Si el backend es local, ruta en disco para pipeline path-based. Else None."""


class LocalStorage(ObjectStorage):
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        clean = key.lstrip("/").replace("..", "_")
        path = (self.root / clean).resolve()
        if not str(path).startswith(str(self.root)):
            raise ValueError(f"key escapes storage root: {key}")
        return path

    def put(self, key: str, data: bytes | BinaryIO, content_type: str = "application/octet-stream") -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, (bytes, bytearray)):
            path.write_bytes(data)
        else:
            with open(path, "wb") as fh:
                shutil.copyfileobj(data, fh)
        return key.lstrip("/")

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()

    def local_path(self, key: str) -> Path | None:
        return self._path(key)


class S3Storage(ObjectStorage):
    """MinIO / Hetzner / AWS via boto3. Opt-in: USE_S3=true."""

    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "us-east-1",
        staging_dir: str | Path = "/tmp/smart-import-s3",
    ):
        try:
            import boto3
        except ImportError as exc:
            raise ImportError(
                "boto3 requerido para S3/MinIO: pip install 'vepathos-smart-import[s3]'"
            ) from exc
        self.bucket = bucket
        self.staging = Path(staging_dir)
        self.staging.mkdir(parents=True, exist_ok=True)
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key or os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=secret_key or os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=region,
        )

    def put(self, key: str, data: bytes | BinaryIO, content_type: str = "application/octet-stream") -> str:
        key = key.lstrip("/")
        extra = {"ContentType": content_type}
        if isinstance(data, (bytes, bytearray)):
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        else:
            self._client.upload_fileobj(data, self.bucket, key, ExtraArgs=extra)
        return key

    def get(self, key: str) -> bytes:
        obj = self._client.get_object(Bucket=self.bucket, Key=key.lstrip("/"))
        return obj["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key.lstrip("/"))
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key.lstrip("/"))

    def local_path(self, key: str) -> Path | None:
        """Materializa a staging para el pipeline path-based."""
        key = key.lstrip("/")
        dest = self.staging / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_bytes(self.get(key))
        return dest


def build_storage_from_env() -> ObjectStorage:
    use_s3 = os.getenv("USE_S3", "false").strip().lower() in {"1", "true", "yes", "on"}
    if use_s3:
        bucket = os.getenv("S3_BUCKET", os.getenv("AWS_S3_BUCKET", "vepathosprod"))
        return S3Storage(
            bucket=bucket,
            endpoint_url=os.getenv("S3_ENDPOINT_URL") or os.getenv("AWS_ENDPOINT_URL"),
            access_key=os.getenv("S3_ACCESS_KEY") or os.getenv("AWS_ACCESS_KEY_ID"),
            secret_key=os.getenv("S3_SECRET_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY"),
            region=os.getenv("S3_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1")),
            staging_dir=os.getenv("SMART_IMPORT_S3_STAGING", "data/tmp/s3-staging"),
        )
    root = os.getenv("SMART_IMPORT_OBJECT_ROOT") or os.getenv("SMART_IMPORT_WORK_DIR", "data/jobs")
    return LocalStorage(root)


def object_key(user: str, job_id: str, kind: str, filename: str) -> str:
    """smart-import/{user}/{job}/raw|normalized|geocoded/{filename}"""
    safe_user = "".join(c if c.isalnum() or c in "-_" else "_" for c in (user or "anon"))[:64]
    return f"smart-import/{safe_user}/{job_id}/{kind}/{Path(filename).name}"
