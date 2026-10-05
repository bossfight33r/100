from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

from clipfactory.storage.base import ObjectNotFound, ObjectStat, validate_key

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


class LocalStorage:
    """ObjectStorage поверх локальной ФС. Запись атомарная (tmp + rename)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # (size, mtime_ns) -> sha256, чтобы не перечитывать большие файлы.
        self._hash_cache: dict[str, tuple[int, int, str]] = {}

    def local_path(self, key: str) -> Path:
        return self.root / validate_key(key)

    def _existing(self, key: str) -> Path:
        path = self.local_path(key)
        if not path.is_file():
            raise ObjectNotFound(key)
        return path

    def exists(self, key: str) -> bool:
        return self.local_path(key).is_file()

    def put_file(self, key: str, path: Path) -> None:
        dest = self.local_path(key)
        src = Path(path).resolve()
        if src == dest:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
        os.close(fd)
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def put_bytes(self, key: str, data: bytes) -> None:
        dest = self.local_path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def get_file(self, key: str, dest: Path) -> Path:
        src = self._existing(key)
        dest = Path(dest)
        if dest.resolve() != src:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        return dest

    def open_read(self, key: str) -> BinaryIO:
        return self._existing(key).open("rb")

    def delete(self, key: str) -> None:
        path = self.local_path(key)
        if path.exists():
            path.unlink()
        self._hash_cache.pop(key, None)

    def stat(self, key: str) -> ObjectStat:
        st = self._existing(key).stat()
        return ObjectStat(key=key, size=st.st_size, mtime=st.st_mtime)

    def checksum(self, key: str) -> str:
        path = self._existing(key)
        st = path.stat()
        cached = self._hash_cache.get(key)
        if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
            return cached[2]
        digest = sha256_file(path)
        self._hash_cache[key] = (st.st_size, st.st_mtime_ns, digest)
        return digest
