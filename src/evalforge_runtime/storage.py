"""Local file storage backend."""

from __future__ import annotations

from pathlib import Path


class LocalStorage:
    """Stores files on the local filesystem."""

    def __init__(self, base_path: str = "./data/files"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    async def put(self, key: str, data: bytes, mime_type: str = "") -> None:
        """Store data at the given key."""
        path = self.base_path / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get(self, key: str) -> bytes:
        """Retrieve data by key."""
        path = self.base_path / key
        if not path.exists():
            raise FileNotFoundError(f"Storage key not found: {key}")
        return path.read_bytes()

    async def exists(self, key: str) -> bool:
        """Check if a key exists."""
        return (self.base_path / key).exists()

    async def delete(self, key: str) -> None:
        """Delete data at the given key."""
        path = self.base_path / key
        if path.exists():
            path.unlink()

    async def size(self, key: str) -> int:
        """Get file size in bytes."""
        path = self.base_path / key
        if not path.exists():
            raise FileNotFoundError(f"Storage key not found: {key}")
        return path.stat().st_size
