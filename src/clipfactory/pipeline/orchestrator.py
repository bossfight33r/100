"""Последовательный запуск этапов job с записью статусов и ошибок в DB."""

from __future__ import annotations

import time

from clipfactory.backends.face.mediapipe import FaceDetectorError
from clipfactory.backends.llm.base import LLMError
from clipfactory.backends.transcriber.base import TranscriberError
from clipfactory.db import Database
from clipfactory.log import get_logger
from clipfactory.media import ffmpeg
from clipfactory.pipeline.captions import CaptionsStage
from clipfactory.pipeline.context import Stage, StageContext, StageError
from clipfactory.pipeline.ingest import IngestStage
from clipfactory.pipeline.reframe import ReframeStage
from clipfactory.pipeline.render import RenderStage
from clipfactory.pipeline.select import SelectStage
from clipfactory.pipeline.transcribe import TranscribeStage
from clipfactory.schemas import (
    STAGE_STATUS,
    ClipMeta,
    ClipRecord,
    Highlights,
    JobStatus,
    StageName,
    StageResult,
)
from clipfactory.storage.base import StorageError

log = get_logger(__name__)


class JobFailed(Exception):
    def __init__(self, job_id: str, stage: StageName | None, cause: BaseException) -> None:
        super().__init__(f"job {job_id} failed at {stage}: {cause}")
        self.job_id = job_id
        self.stage = stage
        self.cause = cause


def default_stages() -> list[Stage]:
    return [
        IngestStage(),
        TranscribeStage(),
        SelectStage(),
        ReframeStage(),
        CaptionsStage(),
        RenderStage(),
    ]


def classify_error(exc: BaseException) -> tuple[str, bool]:
    """(error_type, retryable)."""
    if isinstance(exc, ffmpeg.FFmpegCancelled):
        return "cancelled", True
    if isinstance(exc, ffmpeg.FFmpegTimeout):
        return "ffmpeg_timeout", True
    if isinstance(exc, ffmpeg.FFmpegNotFound):
        return "ffmpeg_missing", False
    if isinstance(exc, ffmpeg.FFmpegError):
        return "ffmpeg_error", False
    if isinstance(exc, StageError):
        return type(exc).__name__, exc.retryable
    if isinstance(exc, LLMError):
        return "llm_error", exc.retryable
    if isinstance(exc, TranscriberError):
        return "transcriber_error", False
    if isinstance(exc, FaceDetectorError):
        return "face_detector_error", False
    if isinstance(exc, StorageError | OSError):
        return "io_error", True
    return type(exc).__name__, True


class Orchestrator:
    def __init__(self, db: Database, stages: list[Stage] | None = None) -> None:
        self.db = db
        self.stages = stages or default_stages()

    def run(self, ctx: StageContext) -> list[StageResult]:
        job_id = ctx.job.id
        results: list[StageResult] = []
        current: StageName | None = None
        try:
            for stage in self.stages:
                current = stage.name
                self.db.set_job_status(job_id, STAGE_STATUS[stage.name])
                started = time.monotonic()
                log.info("stage.start", job_id=job_id, stage=stage.name.value)
                result = stage.run(ctx)
                stage.validate(ctx, result.outputs)
                results.append(result)
                log.info(
                    "stage.done",
                    job_id=job_id,
                    stage=stage.name.value,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    **result.info,
                )
            current = None
            self.sync_clips(ctx)
            self.db.set_job_status(job_id, JobStatus.awaiting_review)
        except Exception as exc:
            error_type, retryable = classify_error(exc)
            self.db.set_job_failed(job_id, current, error_type, str(exc), retryable)
            log.error(
                "job.failed",
                job_id=job_id,
                stage=current.value if current else None,
                error_type=error_type,
                retryable=retryable,
            )
            raise JobFailed(job_id, current, exc) from exc
        return results

    def sync_clips(self, ctx: StageContext) -> None:
        """Записать отрендеренные клипы в DB (статус ревью не трогаем)."""
        highlights = ctx.read_model(ctx.key("highlights.json"), Highlights)
        ids = []
        for cand in highlights.candidates:
            meta_key = ctx.clip_key(cand.id, "meta.json")
            ctx.read_model(meta_key, ClipMeta)
            existing_status = None
            try:
                existing_status = self.db.get_clip(ctx.job.id, cand.id).status
            except Exception:  # noqa: S110 — клипа ещё нет
                pass
            record = ClipRecord(
                job_id=ctx.job.id,
                clip_id=cand.id,
                start=cand.start,
                end=cand.end,
                score=cand.score,
                hook=cand.hook,
                video_key=ctx.clip_key(cand.id, "final.mp4"),
                thumb_key=ctx.clip_key(cand.id, "thumb.jpg"),
                meta_key=meta_key,
            )
            if existing_status is not None:
                record.status = existing_status
            self.db.upsert_clip(record)
            ids.append(cand.id)
        self.db.delete_clips_not_in(ctx.job.id, ids)
