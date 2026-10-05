from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable


class StorageError(Exception):
    pass


class ObjectNotFound(StorageError):
    pass


@dataclass(frozen=True)
class ObjectStat:
    key: str
    size: int
    mtime: float


@runtime_checkable
class ObjectStorage(Protocol):
    """Хранилище артефактов по ключам вида ``jobs/{job_id}/clips/{clip_id}/final.mp4``.

    Pipeline оперирует ключами, а не абсолютными путями, чтобы позже можно было
    подменить реализацию (S3/MinIO) без изменения этапов.
    """

    def exists(self, key: str) -> bool: ...
    def put_file(self, key: str, path: Path) -> None: ...
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def get_file(self, key: str, dest: Path) -> Path: ...
    def open_read(self, key: str) -> BinaryIO: ...
    def delete(self, key: str) -> None: ...
    def stat(self, key: str) -> ObjectStat: ...
    def checksum(self, key: str) -> str: ...


@runtime_checkable
class SupportsLocalPath(Protocol):
    """Оптимизация для локального хранилища: прямой путь без копирования."""

    def local_path(self, key: str) -> Path: ...


def validate_key(key: str) -> str:
    if not key or key.startswith("/") or "\\" in key:
        raise StorageError(f"invalid storage key {key!r}")
    parts = key.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise StorageError(f"invalid storage key {key!r}")
    return key
