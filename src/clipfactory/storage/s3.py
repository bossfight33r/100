"""ObjectStorage поверх S3 / MinIO / любого S3-совместимого хранилища.

sha256 объекта пишется в его метаданные при загрузке, поэтому ``checksum`` —
дешёвый HEAD-запрос, а не скачивание (на этом держится кеш этапов).
Учётные данные — стандартные переменные AWS_* / профиль boto3.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from datetime import UTC
from pathlib import Path
from typing import Any, BinaryIO

from clipfactory.storage.base import ObjectNotFound, ObjectStat, StorageError, validate_key
from clipfactory.storage.local import sha256_file

SHA_META = "sha256"
_CHUNK = 1024 * 1024


class S3Storage:
    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        client: Any = None,
        endpoint_url: str | None = None,
        region: str | None = None,
    ) -> None:
        if client is None:
            try:
                import boto3
            except ImportError as e:  # pragma: no cover
                raise StorageError("boto3 is not installed (uv pip install -e '.[s3]')") from e
            client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        validate_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    @staticmethod
    def _missing(exc: Exception) -> bool:
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        return code in ("404", "NoSuchKey", "NotFound")

    def _head(self, key: str) -> dict:
        try:
            return self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as e:
            if self._missing(e):
                raise ObjectNotFound(key) from e
            raise StorageError(f"s3 head {key}: {e}") from e

    def exists(self, key: str) -> bool:
        try:
            self._head(key)
        except ObjectNotFound:
            return False
        return True

    def put_file(self, key: str, path: Path) -> None:
        digest = sha256_file(Path(path))
        try:
            self.client.upload_file(
                str(path), self.bucket, self._key(key), ExtraArgs={"Metadata": {SHA_META: digest}}
            )
        except Exception as e:
            raise StorageError(f"s3 upload {key}: {e}") from e

    def put_bytes(self, key: str, data: bytes) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(key),
                Body=data,
                Metadata={SHA_META: hashlib.sha256(data).hexdigest()},
            )
        except Exception as e:
            raise StorageError(f"s3 put {key}: {e}") from e

    def get_file(self, key: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
        os.close(fd)
        try:
            self.client.download_file(self.bucket, self._key(key), tmp)
            os.replace(tmp, dest)
        except Exception as e:
            if self._missing(e):
                raise ObjectNotFound(key) from e
            raise StorageError(f"s3 download {key}: {e}") from e
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return dest

    def open_read(self, key: str) -> BinaryIO:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as e:
            if self._missing(e):
                raise ObjectNotFound(key) from e
            raise StorageError(f"s3 get {key}: {e}") from e
        return io.BytesIO(obj["Body"].read())

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as e:
            if not self._missing(e):
                raise StorageError(f"s3 delete {key}: {e}") from e

    def stat(self, key: str) -> ObjectStat:
        head = self._head(key)
        modified = head.get("LastModified")
        mtime = modified.astimezone(UTC).timestamp() if modified else 0.0
        return ObjectStat(key=key, size=int(head.get("ContentLength", 0)), mtime=mtime)

    def checksum(self, key: str) -> str:
        meta = {k.lower(): v for k, v in (self._head(key).get("Metadata") or {}).items()}
        if SHA_META in meta:
            return meta[SHA_META]
        # объект загружен не нами — считаем потоково
        h = hashlib.sha256()
        with self.open_read(key) as f:
            while chunk := f.read(_CHUNK):
                h.update(chunk)
        return h.hexdigest()
