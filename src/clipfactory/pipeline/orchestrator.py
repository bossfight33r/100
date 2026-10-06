"""Последовательный запуск этапов job с записью статусов и ошибок в DB."""

from __future__ import annotations

import json
import threading
import time

from clipfactory.backends.face.mediapipe import FaceDetectorError
from clipfactory.backends.llm.base import LLMError
from clipfactory.backends.transcriber.base import TranscriberError
from clipfactory.db import Database, NotFound
from clipfactory.log import get_logger
from clipfactory.media import ffmpeg
from clipfactory.pipeline.captions import CaptionsStage
from clipfactory.pipeline.context import (
    Stage,
    StageContext,
    StageError,
    ValidationFailed,
    config_hash,
    stable_json,
)
from clipfactory.pipeline.ingest import IngestStage
from clipfactory.pipeline.reframe import ReframeStage
from clipfactory.pipeline.render import RenderStage
from clipfactory.pipeline.select import SelectStage
from clipfactory.pipeline.transcribe import TranscribeStage
from clipfactory.schemas import (
    STAGE_STATUS,
    ClipMeta,
    ClipRecord,
    ClipStatus,
    Highlights,
    JobStatus,
    StageManifest,
    StageName,
    StageResult,
    StageStatus,
    utcnow,
)
from clipfactory.storage.base import StorageError

log = get_logger(__name__)


class JobCancelled(Exception):
    pass


class CancelWatcher:
    """Фоновый опрос флага отмены в DB -> ctx.cancel (ffmpeg и циклы этапов его слушают)."""

    def __init__(self, db: Database, job_id: str, event: threading.Event, interval: float = 1.0):
        self.db, self.job_id, self.event, self.interval = db, job_id, event, interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"cancel-{job_id}", daemon=True)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                if self.db.cancel_requested(self.job_id):
                    self.event.set()
                    return
            except Exception as e:  # DB временно недоступна — не роняем pipeline
                log.debug("cancel_watcher.error", error=str(e))

    def __enter__(self) -> CancelWatcher:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


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
    if isinstance(exc, JobCancelled | ffmpeg.FFmpegCancelled):
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

    # ------------------------------------------------------------ manifests

    @staticmethod
    def manifest_key(ctx: StageContext) -> str:
        return ctx.key("manifest.json")

    def load_manifests(self, ctx: StageContext) -> dict[StageName, StageManifest]:
        key = self.manifest_key(ctx)
        if not ctx.storage.exists(key):
            return {}
        try:
            with ctx.storage.open_read(key) as f:
                raw = json.loads(f.read())
            return {StageName(k): StageManifest.model_validate(v) for k, v in raw.items()}
        except (ValueError, KeyError) as e:
            log.warning("manifest.corrupt", job_id=ctx.job.id, error=str(e))
            return {}

    def save_manifests(self, ctx: StageContext, manifests: dict[StageName, StageManifest]) -> None:
        data = {k.value: v.model_dump(mode="json") for k, v in manifests.items()}
        ctx.storage.put_bytes(self.manifest_key(ctx), stable_json(data))

    @staticmethod
    def input_hashes(stage: Stage, ctx: StageContext) -> dict[str, str]:
        hashes = {key: ctx.storage.checksum(key) for key in stage.input_keys(ctx)}
        fingerprint = getattr(stage, "source_fingerprint", None)
        if fingerprint is not None:
            hashes["source"] = fingerprint(ctx)
        return hashes

    def cache_valid(
        self,
        stage: Stage,
        ctx: StageContext,
        manifest: StageManifest | None,
        cfg_hash: str,
    ) -> tuple[bool, str]:
        """Cache hit: версия, config, входы, наличие и хеши выходов, validate()."""
        if manifest is None:
            return False, "no manifest"
        if manifest.status != StageStatus.completed:
            return False, f"previous status {manifest.status}"
        if manifest.stage_version != stage.version:
            return False, "stage version changed"
        if manifest.config_hash != cfg_hash:
            return False, "config changed"
        try:
            if manifest.input_hashes != self.input_hashes(stage, ctx):
                return False, "inputs changed"
            for key, digest in manifest.output_hashes.items():
                if not ctx.storage.exists(key):
                    return False, f"missing output {key}"
                if ctx.storage.checksum(key) != digest:
                    return False, f"output modified {key}"
            stage.validate(ctx, list(manifest.output_hashes))
        except ValidationFailed as e:
            return False, f"validation failed: {e}"
        except (StorageError, StageError, OSError) as e:
            return False, f"cannot verify: {e}"
        return True, "hit"

    # ------------------------------------------------------------ run

    def run(
        self,
        ctx: StageContext,
        *,
        force_stage: StageName | None = None,
        no_cache: bool = False,
    ) -> list[StageResult]:
        """Выполнить этапы, пропуская валидный кеш. ``force_stage`` перезапускает этап и всё после."""
        job_id = ctx.job.id
        results: list[StageResult] = []
        current: StageName | None = None
        manifests = self.load_manifests(ctx)
        forced = False
        ctx.no_clip_cache = no_cache
        try:
            with CancelWatcher(self.db, job_id, ctx.cancel):
                for stage in self.stages:
                    if ctx.cancel.is_set():
                        raise JobCancelled("cancelled by user")
                    current = stage.name
                    forced = forced or no_cache or stage.name == force_stage
                    self.db.set_job_status(job_id, STAGE_STATUS[stage.name])
                    cfg_hash = config_hash(stage.config(ctx))
                    run_id = self.db.start_stage_run(job_id, stage.name, stage.version, cfg_hash)
                    started = time.monotonic()
                    try:
                        hit, reason = (
                            (False, "forced")
                            if forced
                            else self.cache_valid(stage, ctx, manifests.get(stage.name), cfg_hash)
                        )
                        if hit:
                            manifest = manifests[stage.name]
                            results.append(
                                StageResult(
                                    stage=stage.name,
                                    outputs=list(manifest.output_hashes),
                                    cached=True,
                                )
                            )
                            self.db.finish_stage_run(
                                run_id, StageStatus.skipped.value, cached=True,
                                duration_ms=int((time.monotonic() - started) * 1000),
                            )  # fmt: skip
                            log.info("stage.cached", job_id=job_id, stage=stage.name.value)
                            continue

                        log.info(
                            "stage.start", job_id=job_id, stage=stage.name.value, reason=reason
                        )
                        started_at = utcnow()
                        # входы фиксируем до запуска: этап не должен их менять
                        inputs = self.input_hashes(stage, ctx)
                        manifests.pop(stage.name, None)
                        self.save_manifests(ctx, manifests)
                        result = stage.run(ctx)
                        stage.validate(ctx, result.outputs)
                        manifests[stage.name] = StageManifest(
                            stage=stage.name,
                            stage_version=stage.version,
                            input_hashes=inputs,
                            config_hash=cfg_hash,
                            output_hashes={k: ctx.storage.checksum(k) for k in result.outputs},
                            started_at=started_at,
                            completed_at=utcnow(),
                            status=StageStatus.completed,
                        )
                        self.save_manifests(ctx, manifests)
                    except Exception as exc:
                        error_type, _ = classify_error(exc)
                        self.db.finish_stage_run(
                            run_id, StageStatus.failed.value, error_type=error_type,
                            error_message=str(exc),
                            duration_ms=int((time.monotonic() - started) * 1000),
                        )  # fmt: skip
                        raise
                    duration_ms = int((time.monotonic() - started) * 1000)
                    self.db.finish_stage_run(
                        run_id, StageStatus.completed.value, duration_ms=duration_ms
                    )
                    results.append(result)
                    log.info(
                        "stage.done",
                        job_id=job_id,
                        stage=stage.name.value,
                        duration_ms=duration_ms,
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
        """Записать отрендеренные клипы в DB.

        Статус ревью и правка метаданных сохраняются, только если клип с тем же id
        вырезан из того же места; новый выбор select под старым id ревьюится заново.
        """
        highlights = ctx.read_model(ctx.key("highlights.json"), Highlights)
        ids = []
        for cand in highlights.candidates:
            meta_key = ctx.clip_key(cand.id, "meta.json")
            ctx.read_model(meta_key, ClipMeta)
            existing_status = None
            try:
                existing = self.db.get_clip(ctx.job.id, cand.id)
            except NotFound:
                existing = None
            if existing is not None:
                if (existing.start, existing.end) == (cand.start, cand.end):
                    existing_status = existing.status
                else:
                    # upsert не трогает статус — сбрасываем явно
                    self.db.set_clip_meta_override(ctx.job.id, cand.id, None)
                    self.db.set_clip_status(ctx.job.id, cand.id, ClipStatus.pending_review)
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
