"""Сборка зависимостей и операции над job. Используется CLI, ботом и воркером."""

from __future__ import annotations

import shutil
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from clipfactory.backends.encoder.base import EncoderBackend, select_encoder
from clipfactory.backends.face.base import FaceDetector
from clipfactory.backends.llm.base import LLMProvider
from clipfactory.backends.transcriber.base import Transcriber
from clipfactory.compute.capabilities import TaskRequirements
from clipfactory.config import ConfigError, Settings
from clipfactory.db import Database
from clipfactory.log import get_logger
from clipfactory.pipeline.context import Backends, StageContext
from clipfactory.pipeline.ingest import is_url
from clipfactory.pipeline.orchestrator import Orchestrator
from clipfactory.queue.base import Queue
from clipfactory.queue.inline import InlineQueue
from clipfactory.schemas import Job, JobStatus, ReviewOverrides, StageName, utcnow
from clipfactory.storage.base import ObjectNotFound, ObjectStorage, SupportsLocalPath
from clipfactory.storage.local import LocalStorage

log = get_logger(__name__)


class JobBusy(ValueError):
    """Задача job уже в очереди или выполняется — второй экземпляр не ставим."""


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
    if settings.llm_provider == "ollama":
        from clipfactory.backends.llm.ollama import OllamaLLM

        return OllamaLLM(
            settings.ollama_model, base_url=settings.ollama_url, num_ctx=settings.ollama_num_ctx
        )
    if settings.llm_provider == "openai_compat":
        from clipfactory.backends.llm.openai_compat import OpenAICompatLLM

        if not settings.llm_base_url:
            raise ValueError("CF_LLM_PROVIDER=openai_compat requires CF_LLM_BASE_URL")
        key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
        return OpenAICompatLLM(
            settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=key,
            temperature=settings.llm_temperature,
            json_mode=settings.llm_json_mode,
            token_param=settings.llm_token_param,
        )
    from clipfactory.backends.llm.anthropic import AnthropicLLM

    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    return AnthropicLLM(settings.llm_model, api_key=key, effort=settings.llm_effort)


def llm_config_problem(settings: Settings) -> str | None:
    """Проверка настроек LLM без сети — до ingest/transcribe, а не через 20 минут на select."""
    p = settings.llm_provider
    if p == "anthropic" and not settings.anthropic_api_key:
        return (
            "CF_LLM_PROVIDER=anthropic, но ANTHROPIC_API_KEY пуст. Задайте ключ в .env "
            "или другой провайдер (CF_LLM_PROVIDER=openai_compat + CF_LLM_BASE_URL/"
            "CF_LLM_MODEL/CF_LLM_API_KEY)"
        )
    if p == "openai_compat":
        if not settings.llm_base_url:
            return "CF_LLM_PROVIDER=openai_compat требует CF_LLM_BASE_URL"
        if not settings.llm_model or "llm_model" not in settings.model_fields_set:
            return (
                "CF_LLM_PROVIDER=openai_compat требует CF_LLM_MODEL "
                "(например gemini-3.5-flash-lite) — иначе остаётся модель по умолчанию"
            )
        host = urllib.parse.urlparse(settings.llm_base_url).hostname or ""
        local = host in {"localhost", "127.0.0.1", "::1"}
        if not settings.llm_api_key and not local:
            return f"CF_LLM_API_KEY пуст, а {host} — внешний сервис"
    return None


def build_face(settings: Settings) -> FaceDetector:
    if settings.face_detector == "fake":
        from clipfactory.backends.face.fake import FakeFaceDetector

        return FakeFaceDetector()
    from clipfactory.backends.face.mediapipe import MediaPipeFaceDetector

    return MediaPipeFaceDetector(settings.face_model_path)


def build_encoder(settings: Settings) -> EncoderBackend:
    from clipfactory.media import ffmpeg

    return select_encoder(settings.encoder, ffmpeg.list_encoders(), works=ffmpeg.encoder_works)


def build_storage(settings: Settings) -> ObjectStorage:
    if settings.storage == "s3":
        from clipfactory.storage.s3 import S3Storage

        if not settings.s3_bucket:
            raise ValueError("CF_STORAGE=s3 requires CF_S3_BUCKET")
        return S3Storage(
            settings.s3_bucket,
            prefix=settings.s3_prefix,
            # пустые CF_S3_ENDPOINT_URL= / CF_S3_REGION= из .env — это «не задано», а не ""
            endpoint_url=settings.s3_endpoint_url or None,
            region=settings.s3_region or None,
        )
    return LocalStorage(settings.data_dir)


def backend_ids(settings: Settings) -> dict[str, str]:
    """Идентичность бэкендов из настроек — для config_hash без загрузки моделей."""
    from clipfactory.backends.transcriber.mlx import mlx_available

    tr = settings.transcriber
    if tr == "auto":
        tr = "mlx" if mlx_available() else "faster_whisper"
    tr_model = "fake" if tr == "fake" else settings.whisper_model
    llm = {
        "fake": "fake",
        "ollama": f"ollama/{settings.ollama_model}",
        "openai_compat": f"openai_compat/{settings.llm_base_url}/{settings.llm_model}",
        "anthropic": f"anthropic/{settings.llm_model}/{settings.llm_effort}",
    }[settings.llm_provider]
    face = (
        "fake" if settings.face_detector == "fake" else f"mediapipe/{settings.face_model_path.name}"
    )
    return {"transcriber": f"{tr}/{tr_model}", "llm": llm, "face": face}


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
    storage: ObjectStorage = field(init=False)
    db: Database = field(init=False)

    def __post_init__(self) -> None:
        self.storage = build_storage(self.settings)
        self.db = Database(self.settings.db_target)
        self.db.sync_campaigns(self.settings.campaigns)
        self.db.sync_accounts(self.settings.accounts)

    def backends(self) -> Backends:
        s = self.settings
        return Backends(
            transcriber_factory=self.transcriber_factory or (lambda: build_transcriber(s)),
            llm_factory=self.llm_factory or (lambda: build_llm(s)),
            face_factory=self.face_factory or (lambda: build_face(s)),
            encoder_factory=self.encoder_factory or (lambda: build_encoder(s)),
            ids=backend_ids(s),
        )

    # ------------------------------------------------------------ jobs

    def create_job(self, source: str, campaign_id: str) -> Job:
        self.settings.campaign(campaign_id)  # валидация id
        if self.llm_factory is None and (problem := llm_config_problem(self.settings)):
            raise ConfigError(problem)
        if not is_url(source):
            source = str(Path(source).expanduser().resolve())
        job = Job(
            id=new_job_id(),
            campaign_id=campaign_id,
            source=source,
            require_tags=list(self.settings.job_require_tags),
        )
        return self.db.create_job(job)

    def overrides_key(self, job_id: str) -> str:
        return f"jobs/{job_id}/overrides.json"

    def load_overrides(self, job_id: str) -> ReviewOverrides:
        key = self.overrides_key(job_id)
        if not self.storage.exists(key):
            return ReviewOverrides()
        with self.storage.open_read(key) as f:
            return ReviewOverrides.model_validate_json(f.read())

    def context(self, job: Job) -> StageContext:
        return StageContext(
            job=job,
            campaign=self.settings.campaign(job.campaign_id),
            settings=self.settings,
            storage=self.storage,
            backends=self.backends(),
            overrides=self.load_overrides(job.id),
        )

    def run_job(
        self, job_id: str, *, force_stage: StageName | None = None, no_cache: bool = False
    ) -> Job:
        job = self.db.get_job(job_id)
        try:
            ctx = self.context(job)
        except Exception as e:
            # иначе job навсегда остаётся queued: Orchestrator ещё не начал писать статусы
            self.db.set_job_failed(job_id, None, type(e).__name__, str(e), False)
            raise
        try:
            Orchestrator(self.db).run(ctx, force_stage=force_stage, no_cache=no_cache)
        finally:
            if ctx._scratch is not None:  # копии из удалённого хранилища не копим
                self._upload_logs(ctx._scratch, job_id)
                self._repoint_error_paths(ctx._scratch, job_id)
                shutil.rmtree(ctx._scratch, ignore_errors=True)
        return self.db.get_job(job_id)

    def _upload_logs(self, scratch: Path, job_id: str) -> None:
        """Логи ffmpeg из scratch -> хранилище (jobs/{id}/logs/*), чтобы пережили очистку."""
        logs = scratch / "jobs" / job_id / "logs"
        if not logs.is_dir():
            return
        for f in logs.glob("*.log"):
            try:
                self.storage.put_file(f"jobs/{job_id}/logs/{f.name}", f)
            except Exception as e:  # лог не должен ронять job
                log.warning("logs.upload_failed", job_id=job_id, error=str(e)[:200])

    def _repoint_error_paths(self, scratch: Path, job_id: str) -> None:
        """В тексте ошибки путь к логу во временной папке -> место лога в хранилище."""
        try:
            job = self.db.get_job(job_id)
            prefix = str(scratch / "jobs" / job_id)
            if (
                job.status != JobStatus.failed
                or not job.error_message
                or prefix not in job.error_message
            ):
                return
            message = job.error_message.replace(prefix, self.location(f"jobs/{job_id}"))
            self.db.set_job_failed(
                job_id, job.failed_stage, job.error_type or "error", message, bool(job.retryable)
            )
        except Exception as e:  # из finally: не подменять исходную ошибку и не мешать rmtree
            log.warning("logs.repoint_failed", job_id=job_id, error=str(e)[:200])

    def location(self, key: str) -> str:
        """Человекочитаемое место артефакта: путь на диске или s3://bucket/prefix/key."""
        if isinstance(self.storage, SupportsLocalPath):
            return str(self.storage.local_path(key))
        prefix = f"{self.settings.s3_prefix.strip('/')}/" if self.settings.s3_prefix else ""
        return f"s3://{self.settings.s3_bucket}/{prefix}{key}"

    def materialize(self, key: str) -> Path:
        """Локальный файл для артефакта (публикация, отправка в Telegram).

        LocalStorage — прямой путь; иначе кеш в data_dir/cache/objects, перекачивается,
        если sha256 объекта изменился.
        """
        if isinstance(self.storage, SupportsLocalPath):
            local = self.storage.local_path(key)
            if not local.is_file():
                raise ObjectNotFound(key)
            return local
        path = self.settings.data_dir / "cache" / "objects" / key
        marker = path.with_name(path.name + ".sha256")
        digest = self.storage.checksum(key)
        if not (path.exists() and marker.exists() and marker.read_text() == digest):
            self.storage.get_file(key, path)
            marker.write_text(digest)
        return path

    def materialize_optional(self, key: str | None) -> Path | None:
        """``materialize`` для необязательного артефакта (обложка): нет объекта — None."""
        if not key:
            return None
        try:
            return self.materialize(key)
        except ObjectNotFound:
            return None

    # ------------------------------------------------------------ queue

    @cached_property
    def queue(self) -> Queue:
        if self.settings.queue == "rq":
            from clipfactory.queue.rq import RQQueue

            return RQQueue(self.settings.redis_url)
        from clipfactory.worker import dispatch

        return InlineQueue(lambda task: dispatch(self, task))

    def job_busy(self, job_id: str) -> bool:
        from clipfactory.queue.base import TaskStatus
        from clipfactory.worker import run_job_task_id

        return self.queue.status(run_job_task_id(job_id)) in (TaskStatus.queued, TaskStatus.running)

    def enqueue_job(
        self, job_id: str, *, force_stage: StageName | None = None, no_cache: bool = False
    ) -> str:
        from clipfactory.worker import make_run_task

        if self.job_busy(job_id):
            raise JobBusy(f"job {job_id} is already queued or running")
        self.db.clear_cancel(job_id)
        self.db.set_job_status(job_id, JobStatus.queued)
        task = make_run_task(
            job_id,
            force_stage=force_stage.value if force_stage else None,
            no_cache=no_cache,
            requirements=self.job_requirements(job_id),
        )
        task_id = self.queue.enqueue(task)
        self._warn_if_no_worker(task.requirements)
        return task_id

    def job_requirements(self, job: Job | str) -> TaskRequirements:
        """Требования job к воркеру — сохранены при создании (CF_JOB_REQUIRE_TAGS того, кто ставил).

        Не берём из настроек текущего процесса: воркер, восстанавливающий чужую GPU-job,
        иначе отправил бы её в общую очередь. Принимает уже загруженный Job или его id.
        """
        if isinstance(job, str):
            job = self.db.get_job(job)
        return TaskRequirements(tags=job.require_tags)

    def _warn_if_no_worker(self, req: TaskRequirements) -> None:
        conn = getattr(self.queue, "connection", None)
        if conn is None:
            return
        from clipfactory.compute.routing import eligible_workers, live_workers

        try:
            if not eligible_workers(req, live_workers(conn)):
                log.warning("queue.no_eligible_worker", tags=req.tags)
        except Exception as e:  # диагностика не должна мешать постановке
            log.debug("queue.worker_check_failed", error=str(e))

    def cancel_job(self, job_id: str) -> str:
        """Отменить job: снять из очереди или попросить работающий pipeline остановиться."""
        from clipfactory.worker import ACTIVE_STATUSES, run_job_task_id

        job = self.db.get_job(job_id)
        if job.status == JobStatus.queued and self.queue.cancel(run_job_task_id(job_id)):
            self.db.set_job_failed(job_id, None, "cancelled", "cancelled before start", True)
            return "dequeued"
        if job.status in (JobStatus.queued, *ACTIVE_STATUSES):
            self.db.request_cancel(job_id)
            return "requested"
        raise ValueError(f"job {job_id} is {job.status.value}; nothing to cancel")

    def retry_job(self, job_id: str, *, force_stage: StageName | None = None) -> str:
        """Повторить job: этапы с валидным манифестом не выполняются заново."""
        job = self.db.get_job(job_id)
        if job.status not in (JobStatus.failed, JobStatus.awaiting_review, JobStatus.queued):
            if force_stage is None:
                raise ValueError(f"job {job_id} is {job.status.value}; nothing to retry")
        return self.enqueue_job(job_id, force_stage=force_stage)

    def job_summary(self, job_id: str) -> dict:
        job = self.db.get_job(job_id)
        clips = self.db.list_clips(job_id)
        return {
            "job": job.model_dump(mode="json"),
            "clips": [c.model_dump(mode="json") for c in clips],
            "stage_runs": self.db.stage_runs(job_id),
            "dir": self.location(f"jobs/{job_id}"),
        }


def is_terminal(status: JobStatus) -> bool:
    return status in (JobStatus.awaiting_review, JobStatus.failed, JobStatus.published)
