"""Единственное место контрактов между этапами, хранилищем, ботом и публикацией."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


# ---------------------------------------------------------------- transcript


class Word(_Model):
    text: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    probability: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _order(self) -> Word:
        if self.end < self.start:
            raise ValueError(f"word end {self.end} < start {self.start}")
        return self


class Segment(_Model):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    words: list[Word] = Field(default_factory=list)


class Transcript(_Model):
    language: str
    model: str
    duration: float = Field(ge=0)
    words: list[Word]
    segments: list[Segment]


# ---------------------------------------------------------------- config objects


_TIME_WINDOW = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


class Platform(StrEnum):
    youtube = "youtube"
    tiktok = "tiktok"
    instagram = "instagram"


class Account(_Model):
    id: str
    platform: Platform
    name: str
    token_ref: str | None = None
    daily_limit: int = Field(default=3, ge=0)
    posting_windows: list[str] = Field(default_factory=lambda: ["10:00-22:00"])
    timezone: str = "UTC"

    @field_validator("posting_windows")
    @classmethod
    def _windows(cls, v: list[str]) -> list[str]:
        for w in v:
            if not _TIME_WINDOW.match(w):
                raise ValueError(f"posting window must be HH:MM-HH:MM, got {w!r}")
            a, b = w.split("-")
            if a >= b:
                raise ValueError(f"posting window start must be before end: {w!r}")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone {v!r}") from e
        return v

    @field_validator("token_ref")
    @classmethod
    def _token_ref(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[A-Za-z0-9_.-]+", v):
            raise ValueError("token_ref must be a plain identifier, not a path or a secret")
        return v


class SelectionMode(StrEnum):
    transcript = "transcript"  # LLM по тексту речи (подкасты, интервью)
    signals = "signals"  # пики звука и YouTube «Most replayed» (игры, стримы) — ADR-0014
    hybrid = "hybrid"  # LLM по речи + подсказки пиков сигналов и их вес в оценке — ADR-0017
    whole = "whole"  # исходник целиком одним клипом: клипы Twitch/YouTube (уже хайлайт)


class Layout(StrEnum):
    crop = "crop"  # кроп 9:16 по лицу/центру (говорящие головы)
    fit_blur = "fit_blur"  # весь кадр по центру, сверху и снизу размытый фон (геймплей)


class Campaign(_Model):
    id: str
    name: str
    rate_per_1k_views: float = Field(ge=0)
    platforms: list[Platform]
    clip_min_sec: float = Field(default=20, gt=0)
    clip_max_sec: float = Field(default=60, gt=0)
    clip_count: int = Field(default=3, ge=1)
    language: str | None = None
    must_include_tags: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    notes: str = ""
    accounts: list[str] = Field(default_factory=list)
    selection: SelectionMode = SelectionMode.transcript
    transcribe: bool = True  # False — без Whisper и без субтитров (только для selection: signals)
    layout: Layout = Layout.crop
    fit_zoom: float = Field(default=1.0, ge=1.0, le=2.0)  # fit_blur: >1 — крупнее, края срезаются
    title_overlay: bool = False  # заголовок клипа крупно сверху кадра (игровые shorts)

    @model_validator(mode="after")
    def _durations(self) -> Campaign:
        if self.clip_min_sec > self.clip_max_sec:
            raise ValueError("clip_min_sec must be <= clip_max_sec")
        if not self.transcribe and self.selection not in (
            SelectionMode.signals,
            SelectionMode.whole,
        ):
            raise ValueError("transcribe: false requires selection: signals or whole")
        return self


# ---------------------------------------------------------------- source metadata


class HeatPoint(_Model):
    """Точка кривой YouTube «Most replayed» (поле heatmap у yt-dlp), value 0..1."""

    start_time: float = Field(ge=0)
    end_time: float = Field(ge=0)
    value: float = Field(ge=0, le=1)


class Chapter(_Model):
    start_time: float = Field(ge=0)
    end_time: float = Field(ge=0)
    title: str = ""


class SourceInfo(_Model):
    """source.info.json: метаданные площадки-источника (сейчас — из yt-dlp)."""

    title: str = ""
    channel: str = ""
    duration: float | None = None
    view_count: int | None = None
    heatmap: list[HeatPoint] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    was_live: bool = False  # запись стрима — у неё может быть чат


class SignalsTrace(_Model):
    """signals.json: ряды сигналов select (signals) для отладки и подстройки весов."""

    hop: float = Field(gt=0)
    weights: dict[str, float]
    series: dict[str, list[float]]
    fused: list[float]


class ChatActivity(_Model):
    """chat.json: сообщений чата записи стрима на каждые hop секунд (ADR-0016)."""

    hop: float = Field(gt=0)
    counts: list[int]


class WatchSource(_Model):
    """config/watch.yaml: канал/плейлист, новые видео которого сами идут в нарезку (cf watch)."""

    url: str
    campaign: str
    min_views: int = Field(default=0, ge=0)
    max_new: int = Field(default=3, ge=1)  # сколько новых видео ставить за одну проверку
    min_minutes: float = Field(default=5, ge=0)
    max_minutes: float = Field(default=240, gt=0)
    limit: int = Field(default=15, ge=1, le=200)  # сколько последних видео смотреть

    @field_validator("url")
    @classmethod
    def _http(cls, v: str) -> str:
        if not re.match(r"^https?://\S+$", v):
            raise ValueError("url must be http(s)")
        return v


class SourceCandidate(_Model):
    """Видео-кандидат из `cf discover` (канал/плейлист)."""

    url: str
    title: str = ""
    channel: str = ""
    duration: float | None = None
    view_count: int | None = None
    heatmap_points: int | None = None  # None — не проверялось
    already_processed: bool = False


# ---------------------------------------------------------------- selection / clips


class ClipCandidate(_Model):
    id: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    score: int = Field(ge=0, le=100)
    hook: str = ""
    reason: str = ""
    title: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start

    @model_validator(mode="after")
    def _order(self) -> ClipCandidate:
        if self.end <= self.start:
            raise ValueError("clip end must be after start")
        return self


class Highlights(_Model):
    candidates: list[ClipCandidate]


class PlatformClipMeta(_Model):
    platform: Platform
    title: str
    description: str
    hashtags: list[str] = Field(default_factory=list)


class ClipMeta(_Model):
    """Содержимое clips/{id}/meta.json."""

    clip_id: str
    candidate: ClipCandidate
    platforms: list[PlatformClipMeta]
    duration: float
    meta_hash: str = (
        ""  # hash(кандидат + конфиг метаданных) — для переиспользования при перерендере
    )


class CropKeyframe(_Model):
    t: float = Field(ge=0)
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(gt=0)
    h: int = Field(gt=0)


class ReframePlan(_Model):
    source_width: int
    source_height: int
    target_width: int = 1080
    target_height: int = 1920
    keyframes: list[CropKeyframe]

    @model_validator(mode="after")
    def _check(self) -> ReframePlan:
        if not self.keyframes:
            raise ValueError("reframe plan needs at least one keyframe")
        ts = [k.t for k in self.keyframes]
        if ts != sorted(ts) or ts[0] != 0:
            raise ValueError("keyframes must be sorted and start at t=0")
        for k in self.keyframes:
            if k.x + k.w > self.source_width or k.y + k.h > self.source_height:
                raise ValueError(f"crop {k} is outside the source frame")
        return self


class RenderedClip(_Model):
    clip_id: str
    video_key: str
    thumb_key: str
    meta_key: str
    duration: float


# ---------------------------------------------------------------- publishing / tracking


class PublicationStatus(StrEnum):
    scheduled = "scheduled"
    publishing = "publishing"
    published = "published"
    exported = "exported"
    failed = "failed"


class Publication(_Model):
    id: str
    job_id: str
    clip_id: str
    campaign_id: str
    platform: Platform
    account_id: str
    external_id: str | None = None
    url: str | None = None
    scheduled_at: datetime | None = None
    published_at: datetime | None = None
    status: PublicationStatus = PublicationStatus.scheduled
    error: str | None = None


class StatsSnapshot(_Model):
    publication_id: str
    views: int = Field(ge=0)
    likes: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)
    collected_at: datetime = Field(default_factory=utcnow)
    source: str = "api"


# ---------------------------------------------------------------- jobs


class JobStatus(StrEnum):
    queued = "queued"
    ingesting = "ingesting"
    transcribing = "transcribing"
    selecting = "selecting"
    reframing = "reframing"
    captioning = "captioning"
    rendering = "rendering"
    awaiting_review = "awaiting_review"
    scheduled = "scheduled"
    publishing = "publishing"
    published = "published"
    failed = "failed"


class StageName(StrEnum):
    ingest = "ingest"
    transcribe = "transcribe"
    select = "select"
    reframe = "reframe"
    captions = "captions"
    render = "render"


STAGE_ORDER: list[StageName] = list(StageName)

STAGE_STATUS: dict[StageName, JobStatus] = {
    StageName.ingest: JobStatus.ingesting,
    StageName.transcribe: JobStatus.transcribing,
    StageName.select: JobStatus.selecting,
    StageName.reframe: JobStatus.reframing,
    StageName.captions: JobStatus.captioning,
    StageName.render: JobStatus.rendering,
}


class Job(_Model):
    id: str
    campaign_id: str
    source: str
    status: JobStatus = JobStatus.queued
    failed_stage: StageName | None = None
    error_type: str | None = None
    error_message: str | None = None
    retryable: bool | None = None
    require_tags: list[str] = Field(default_factory=list)  # к воркеру (маршрутизация)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ClipStatus(StrEnum):
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"


class ClipRecord(_Model):
    job_id: str
    clip_id: str
    status: ClipStatus = ClipStatus.pending_review
    start: float
    end: float
    score: int
    hook: str = ""
    video_key: str | None = None
    thumb_key: str | None = None
    meta_key: str | None = None
    meta_override: list[PlatformClipMeta] | None = None


class ReviewActionType(StrEnum):
    approve = "approve"
    reject = "reject"
    edit_metadata = "edit_metadata"
    rerender_captions = "rerender_captions"
    rerender_crop = "rerender_crop"


class ReviewAction(_Model):
    job_id: str
    clip_id: str
    action: ReviewActionType
    payload: dict[str, Any] = Field(default_factory=dict)
    actor: str = "cli"
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- stage execution


class StageStatus(StrEnum):
    completed = "completed"
    failed = "failed"
    skipped = "skipped"


class StageManifest(_Model):
    stage: StageName
    stage_version: int
    input_hashes: dict[str, str]
    config_hash: str
    output_hashes: dict[str, str]
    started_at: datetime
    completed_at: datetime | None = None
    status: StageStatus


class StageResult(_Model):
    stage: StageName
    outputs: list[str]
    cached: bool = False
    info: dict[str, Any] = Field(default_factory=dict)


class ReviewOverrides(_Model):
    """Пользовательский ввод из ревью, влияющий на этапы (overrides.json, пишет review)."""

    crop_center_x: dict[str, float] = Field(default_factory=dict)
    caption_nonce: dict[str, int] = Field(default_factory=dict)
