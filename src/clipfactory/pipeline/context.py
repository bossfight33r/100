"""Контекст выполнения этапа и базовые ошибки.

Этап видит только StageContext: ключи хранилища, контракты, бэкенды. Никаких
абсолютных путей вне контекста, никаких импортов CLI/бота/rq.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from clipfactory.backends.encoder.base import EncoderBackend
from clipfactory.backends.face.base import FaceDetector
from clipfactory.backends.llm.base import LLMProvider
from clipfactory.backends.transcriber.base import Transcriber
from clipfactory.config import Settings
from clipfactory.schemas import Campaign, Job, ReviewOverrides, StageName, StageResult
from clipfactory.storage.base import ObjectStorage, SupportsLocalPath

M = TypeVar("M", bound=BaseModel)


class StageError(Exception):
    """Ошибка этапа с признаком, имеет ли смысл повтор без изменений."""

    retryable: bool = False

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class SourceError(StageError):
    pass


class NoSpeechError(StageError):
    pass


class NoHighlightsError(StageError):
    pass


class ValidationFailed(StageError):
    pass


@dataclass
class Backends:
    """Ленивые фабрики бэкендов: тяжёлые модели грузятся, только если этап их вызвал."""

    transcriber_factory: Callable[[], Transcriber]
    llm_factory: Callable[[], LLMProvider]
    face_factory: Callable[[], FaceDetector]
    encoder_factory: Callable[[], EncoderBackend]
    _cache: dict[str, Any] = field(default_factory=dict)

    def _get(self, name: str, factory: Callable[[], Any]) -> Any:
        if name not in self._cache:
            self._cache[name] = factory()
        return self._cache[name]

    @property
    def transcriber(self) -> Transcriber:
        return self._get("transcriber", self.transcriber_factory)

    @property
    def llm(self) -> LLMProvider:
        return self._get("llm", self.llm_factory)

    @property
    def face(self) -> FaceDetector:
        return self._get("face", self.face_factory)

    @property
    def encoder(self) -> EncoderBackend:
        return self._get("encoder", self.encoder_factory)


def stable_json(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, default=str).encode()


def config_hash(data: Any) -> str:
    return hashlib.sha256(stable_json(data)).hexdigest()


@dataclass
class StageContext:
    job: Job
    campaign: Campaign
    settings: Settings
    storage: ObjectStorage
    backends: Backends
    overrides: ReviewOverrides = field(default_factory=ReviewOverrides)
    cancel: threading.Event = field(default_factory=threading.Event)
    _scratch: Path | None = None

    # ------------------------------------------------------------- keys

    def key(self, name: str) -> str:
        return f"jobs/{self.job.id}/{name}"

    def clip_key(self, clip_id: str, name: str) -> str:
        return f"jobs/{self.job.id}/clips/{clip_id}/{name}"

    def log_key(self, stage: StageName | str, suffix: str = "") -> str:
        return self.key(f"logs/{stage}{suffix}.log")

    # ------------------------------------------------------------- local files

    @property
    def scratch(self) -> Path:
        if self._scratch is None:
            self._scratch = Path(tempfile.mkdtemp(prefix=f"cf-{self.job.id}-"))
        return self._scratch

    def local_path(self, key: str) -> Path:
        """Путь на диске для чтения или записи артефакта.

        LocalStorage отдаёт прямой путь; для удалённого хранилища — копия в scratch
        (после записи нужно вызвать ``commit``).
        """
        if isinstance(self.storage, SupportsLocalPath):
            path = self.storage.local_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        path = self.scratch / key
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() and self.storage.exists(key):
            self.storage.get_file(key, path)
        return path

    def commit(self, key: str, path: Path) -> None:
        self.storage.put_file(key, path)

    # ------------------------------------------------------------- models

    def write_model(self, key: str, model: BaseModel) -> None:
        data = model.model_dump(mode="json")
        self.storage.put_bytes(key, stable_json(data))

    def read_model(self, key: str, cls: type[M]) -> M:
        with self.storage.open_read(key) as f:
            return cls.model_validate_json(f.read())


class Stage(Protocol):
    name: StageName
    version: int

    def config(self, ctx: StageContext) -> dict[str, Any]:
        """Параметры, влияющие на результат (идут в config_hash)."""
        ...

    def input_keys(self, ctx: StageContext) -> list[str]:
        """Ключи артефактов-входов (идут в input_hashes)."""
        ...

    def run(self, ctx: StageContext) -> StageResult: ...

    def validate(self, ctx: StageContext, outputs: list[str]) -> None:
        """Проверка выходов; бросает ValidationFailed."""
        ...
