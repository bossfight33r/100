"""Сборка зависимостей и операции над job. Используется CLI, ботом и воркером."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from clipfactory.backends.encoder.base import EncoderBackend, select_encoder
from clipfactory.backends.face.base import FaceDetector
from clipfactory.backends.llm.base import LLMProvider
from clipfactory.backends.transcriber.base import Transcriber
from clipfactory.config import Settings
from clipfactory.db import Database
from clipfactory.pipeline.context import Backends, StageContext
from clipfactory.pipeline.ingest import is_url
from clipfactory.pipeline.orchestrator import Orchestrator
from clipfactory.schemas import Job, JobStatus, utcnow
from clipfactory.storage.local import LocalStorage


def build_transcriber(settings: Settings) -> Transcriber:
    from clipfactory.backends.transcriber.mlx import mlx_available

    choice = settings.transcriber
    if choice == "auto":
        choice = "mlx" if mlx_available() else "faster_whisper"
    if choice == "fake":
        from clipfactory.backends.transcriber.fake import FakeTranscriber

        return FakeTranscriber()
    if choice == "mlx":
        from clipfactory.backends.transcriber.mlx import MLXWhisperTranscriber

        return MLXWhisperTranscriber(settings.whisper_model)
    from clipfactory.backends.transcriber.faster_whisper import FasterWhisperTranscriber

    return FasterWhisperTranscriber(settings.whisper_model)


def build_llm(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "fake":
        from clipfactory.backends.llm.fake import FakeLLM

        return FakeLLM()
    from clipfactory.backends.llm.anthropic import AnthropicLLM

    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    return AnthropicLLM(settings.llm_model, api_key=key, effort=settings.llm_effort)


def build_face(settings: Settings) -> FaceDetector:
    if settings.face_detector == "fake":
        from clipfactory.backends.face.fake import FakeFaceDetector

        return FakeFaceDetector()
    from clipfactory.backends.face.mediapipe import MediaPipeFaceDetector

    return MediaPipeFaceDetector(settings.face_model_path)


def build_encoder(settings: Settings) -> EncoderBackend:
    from clipfactory.media import ffmpeg

    return select_encoder(settings.encoder, ffmpeg.list_encoders())


def new_job_id() -> str:
    return f"{utcnow():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


@dataclass
class App:
    """Контейнер зависимостей. В тестах фабрики бэкендов подменяются fake."""

    settings: Settings
    transcriber_factory: Callable[[], Transcriber] | None = None
    llm_factory: Callable[[], LLMProvider] | None = None
    face_factory: Callable[[], FaceDetector] | None = None
    encoder_factory: Callable[[], EncoderBackend] | None = None
    storage: LocalStorage = field(init=False)
    db: Database = field(init=False)

    def __post_init__(self) -> None:
        self.storage = LocalStorage(self.settings.data_dir)
        self.db = Database(self.settings.db_path)
        self.db.sync_campaigns(self.settings.campaigns)
        self.db.sync_accounts(self.settings.accounts)

    def backends(self) -> Backends:
        s = self.settings
        return Backends(
            transcriber_factory=self.transcriber_factory or (lambda: build_transcriber(s)),
            llm_factory=self.llm_factory or (lambda: build_llm(s)),
            face_factory=self.face_factory or (lambda: build_face(s)),
            encoder_factory=self.encoder_factory or (lambda: build_encoder(s)),
        )

    # ------------------------------------------------------------ jobs

    def create_job(self, source: str, campaign_id: str) -> Job:
        self.settings.campaign(campaign_id)  # валидация id
        if not is_url(source):
            source = str(Path(source).expanduser().resolve())
        job = Job(id=new_job_id(), campaign_id=campaign_id, source=source)
        return self.db.create_job(job)

    def context(self, job: Job) -> StageContext:
        return StageContext(
            job=job,
            campaign=self.settings.campaign(job.campaign_id),
            settings=self.settings,
            storage=self.storage,
            backends=self.backends(),
        )

    def run_job(self, job_id: str) -> Job:
        job = self.db.get_job(job_id)
        ctx = self.context(job)
        Orchestrator(self.db).run(ctx)
        return self.db.get_job(job_id)

    def job_summary(self, job_id: str) -> dict:
        job = self.db.get_job(job_id)
        clips = self.db.list_clips(job_id)
        return {
            "job": job.model_dump(mode="json"),
            "clips": [c.model_dump(mode="json") for c in clips],
            "dir": str(self.storage.local_path(f"jobs/{job_id}/source.mp4").parent),
        }


def is_terminal(status: JobStatus) -> bool:
    return status in (JobStatus.awaiting_review, JobStatus.failed, JobStatus.published)
