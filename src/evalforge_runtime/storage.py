"""Storage backends for file persistence."""

from __future__ import annotations

import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path


class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    async def put(self, key: str, data: bytes, mime_type: str = "") -> None:
        """Store data at the given key."""

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """Retrieve data by key."""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Check if a key exists."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete data at the given key."""

    @abstractmethod
    async def size(self, key: str) -> int:
        """Get file size in bytes."""


class LocalStorage(StorageBackend):
    """Stores files on the local filesystem."""

    def __init__(self, base_path: str = "./data/files"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    async def put(self, key: str, data: bytes, mime_type: str = "") -> None:
        path = self.base_path / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get(self, key: str) -> bytes:
        path = self.base_path / key
        if not path.exists():
            raise FileNotFoundError(f"Storage key not found: {key}")
        return path.read_bytes()

    async def exists(self, key: str) -> bool:
        return (self.base_path / key).exists()

    async def delete(self, key: str) -> None:
        path = self.base_path / key
        if path.exists():
            path.unlink()

    async def size(self, key: str) -> int:
        path = self.base_path / key
        if not path.exists():
            raise FileNotFoundError(f"Storage key not found: {key}")
        return path.stat().st_size


class NoneStorage(StorageBackend):
    """Temporary storage that auto-cleans after execution completes.

    Files are stored in a temp directory, grouped by execution ID.
    Call cleanup(execution_id) after processing to remove all files.
    """

    def __init__(self):
        self._base = Path(tempfile.mkdtemp(prefix="evalforge_none_"))

    async def put(self, key: str, data: bytes, mime_type: str = "") -> None:
        path = self._base / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get(self, key: str) -> bytes:
        path = self._base / key
        if not path.exists():
            raise FileNotFoundError(f"Storage key not found: {key}")
        return path.read_bytes()

    async def exists(self, key: str) -> bool:
        return (self._base / key).exists()

    async def delete(self, key: str) -> None:
        path = self._base / key
        if path.exists():
            path.unlink()

    async def size(self, key: str) -> int:
        path = self._base / key
        if not path.exists():
            raise FileNotFoundError(f"Storage key not found: {key}")
        return path.stat().st_size

    def cleanup(self, execution_id: str) -> None:
        """Delete all files for the given execution ID."""
        exec_dir = self._base / "executions" / execution_id
        if exec_dir.exists():
            shutil.rmtree(exec_dir, ignore_errors=True)


class S3Storage(StorageBackend):
    """S3-compatible storage using aioboto3.

    Reads config from environment: S3_BUCKET, AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY, AWS_REGION.
    """

    def __init__(self, bucket: str | None = None, region: str | None = None):
        self.bucket = bucket or os.environ.get("S3_BUCKET", "")
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        if not self.bucket:
            raise ValueError("S3 bucket name is required (set S3_BUCKET env var or storage.bucket in config)")

    def _get_session(self):
        import aioboto3
        return aioboto3.Session(
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
            region_name=self.region,
        )

    async def put(self, key: str, data: bytes, mime_type: str = "") -> None:
        session = self._get_session()
        async with session.client("s3") as s3:
            kwargs: dict = {"Bucket": self.bucket, "Key": key, "Body": data}
            if mime_type:
                kwargs["ContentType"] = mime_type
            await s3.put_object(**kwargs)

    async def get(self, key: str) -> bytes:
        session = self._get_session()
        async with session.client("s3") as s3:
            resp = await s3.get_object(Bucket=self.bucket, Key=key)
            return await resp["Body"].read()

    async def exists(self, key: str) -> bool:
        session = self._get_session()
        async with session.client("s3") as s3:
            try:
                await s3.head_object(Bucket=self.bucket, Key=key)
                return True
            except Exception:
                return False

    async def delete(self, key: str) -> None:
        session = self._get_session()
        async with session.client("s3") as s3:
            await s3.delete_object(Bucket=self.bucket, Key=key)

    async def size(self, key: str) -> int:
        session = self._get_session()
        async with session.client("s3") as s3:
            resp = await s3.head_object(Bucket=self.bucket, Key=key)
            return resp["ContentLength"]


class GCPStorage(StorageBackend):
    """GCP Cloud Storage backend (stub)."""

    def __init__(self, **kwargs):
        raise NotImplementedError("GCP Cloud Storage support coming soon")

    async def put(self, key: str, data: bytes, mime_type: str = "") -> None:
        raise NotImplementedError

    async def get(self, key: str) -> bytes:
        raise NotImplementedError

    async def exists(self, key: str) -> bool:
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        raise NotImplementedError

    async def size(self, key: str) -> int:
        raise NotImplementedError


class AzureBlobStorage(StorageBackend):
    """Azure Blob Storage backend (stub)."""

    def __init__(self, **kwargs):
        raise NotImplementedError("Azure Blob Storage support coming soon")

    async def put(self, key: str, data: bytes, mime_type: str = "") -> None:
        raise NotImplementedError

    async def get(self, key: str) -> bytes:
        raise NotImplementedError

    async def exists(self, key: str) -> bool:
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        raise NotImplementedError

    async def size(self, key: str) -> int:
        raise NotImplementedError


def create_storage(config) -> StorageBackend:
    """Factory: create a storage backend from StorageConfig.

    Args:
        config: A StorageConfig instance with type, path, bucket, region fields.
    """
    storage_type = config.type

    if storage_type == "none":
        return NoneStorage()
    elif storage_type == "local":
        return LocalStorage(config.path)
    elif storage_type == "s3":
        return S3Storage(bucket=config.bucket, region=config.region)
    elif storage_type == "gcp":
        return GCPStorage()
    elif storage_type == "azure":
        return AzureBlobStorage()
    else:
        raise ValueError(f"Unknown storage type: {storage_type}")
