from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from clipfactory.schemas import Account, Platform, PlatformClipMeta, PublicationStatus


class PublishError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class PublishRequest:
    publication_id: str
    account: Account
    video_path: Path
    thumb_path: Path | None
    meta: PlatformClipMeta
    scheduled_at: datetime | None


@dataclass(frozen=True)
class PublishResult:
    status: PublicationStatus
    external_id: str | None = None
    url: str | None = None


@runtime_checkable
class Publisher(Protocol):
    """Публикация на СОБСТВЕННЫЙ аккаунт через официальный API или экспорт для ручной заливки."""

    platform: Platform

    def publish(self, req: PublishRequest) -> PublishResult: ...


def caption_text(meta: PlatformClipMeta) -> str:
    parts = [meta.title.strip(), meta.description.strip(), " ".join(meta.hashtags)]
    return "\n\n".join(p for p in parts if p)
