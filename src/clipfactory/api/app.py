"""Локальный HTTP-API для десктоп-приложения (и любого другого клиента).

Тонкий слой над готовыми сервисами (App, ReviewService, PublishService, отчёты):
никакой бизнес-логики здесь нет. Слушает только 127.0.0.1, все запросы требуют
Bearer-токен (CF_API_TOKEN). Видео и обложки можно запрашивать с ``?token=`` —
плееры не умеют заголовки; access-лог сервера выключен, чтобы токен не попадал в логи.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from clipfactory import __version__
from clipfactory.config import ConfigError
from clipfactory.db import NotFound
from clipfactory.log import get_logger
from clipfactory.pipeline.publish import PublishService, PublishServiceError
from clipfactory.pipeline.review import ReviewError, ReviewService
from clipfactory.publish.scheduler import SchedulingError
from clipfactory.schemas import JobStatus, Platform, StageName
from clipfactory.services import JobBusy
from clipfactory.storage.base import ObjectNotFound
from clipfactory.track.collector import TrackError, add_manual
from clipfactory.track.report import build_report

log = get_logger(__name__)


# ---------------------------------------------------------------- request bodies


class NewJob(BaseModel):
    source: str = Field(min_length=1, description="Абсолютный путь к файлу или http(s) URL")
    campaign_id: str


class EditMetadata(BaseModel):
    title: str | None = None
    description: str | None = None
    hashtags: list[str] | None = None
    platform: Platform | None = None


class Reject(BaseModel):
    reason: str = ""


class CropRequest(BaseModel):
    center_x: float | None = Field(
        default=None, ge=0, le=1, description="Центр кропа 0..1; null — вернуть авто"
    )


class ManualStats(BaseModel):
    views: int = Field(ge=0)
    likes: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)


class RetryRequest(BaseModel):
    force_stage: StageName | None = None


# ---------------------------------------------------------------- app factory


def create_app(app: Any, token: str) -> FastAPI:
    """``app`` — clipfactory.services.App. Токен обязателен (пустой — ошибка конфигурации)."""
    if not token or len(token) < 16:
        raise ValueError("API token must be at least 16 characters")
    api = FastAPI(title="ClipFactory API", version=__version__, docs_url=None, redoc_url=None)

    def check_token(supplied: str | None) -> None:
        if supplied is None or not secrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="invalid or missing token")

    def auth(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, value = (authorization or "").partition(" ")
        check_token(value if scheme.lower() == "bearer" else None)

    def media_auth(
        authorization: Annotated[str | None, Header()] = None,
        token_q: Annotated[str | None, Query(alias="token")] = None,
    ) -> None:
        scheme, _, value = (authorization or "").partition(" ")
        check_token(value if scheme.lower() == "bearer" else token_q)

    protected = [Depends(auth)]

    # --- ошибки сервисов -> понятные HTTP-коды
    def error(status: int, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    @api.exception_handler(NotFound)
    async def _nf(_: Request, exc: NotFound) -> JSONResponse:
        return error(404, exc)

    @api.exception_handler(ObjectNotFound)
    async def _onf(_: Request, exc: ObjectNotFound) -> JSONResponse:
        return error(404, exc)

    @api.exception_handler(JobBusy)
    async def _busy(_: Request, exc: JobBusy) -> JSONResponse:
        return error(409, exc)

    for exc_type in (ReviewError, PublishServiceError, SchedulingError, TrackError):

        @api.exception_handler(exc_type)
        async def _conflict(_: Request, exc: Exception) -> JSONResponse:
            return error(409, exc)

    @api.exception_handler(ConfigError)
    async def _cfg(_: Request, exc: ConfigError) -> JSONResponse:
        return error(400, exc)

    @api.exception_handler(ValueError)
    async def _val(_: Request, exc: ValueError) -> JSONResponse:
        return error(400, exc)

    # ------------------------------------------------------------ system

    @api.get("/health")
    def health() -> dict[str, Any]:  # без токена: приложение проверяет, что сервер поднялся
        return {"status": "ok", "version": __version__}

    @api.get("/capabilities", dependencies=protected)
    def capabilities() -> dict[str, Any]:
        from clipfactory.compute.capabilities import detect

        return detect().model_dump()

    @api.get("/campaigns", dependencies=protected)
    def campaigns() -> list[dict[str, Any]]:
        return [c.model_dump(mode="json") for c in app.settings.campaigns.values()]

    @api.get("/accounts", dependencies=protected)
    def accounts() -> list[dict[str, Any]]:
        return [a.model_dump(mode="json") for a in app.settings.accounts.values()]

    # ------------------------------------------------------------ jobs

    @api.get("/jobs", dependencies=protected)
    def list_jobs(status: JobStatus | None = None, limit: int = Query(50, ge=1, le=200)):
        jobs = app.db.list_jobs([status] if status else None, limit=limit)
        return [j.model_dump(mode="json") for j in jobs]

    @api.post("/jobs", status_code=202, dependencies=protected)
    def create_job(body: NewJob, background: BackgroundTasks) -> dict[str, Any]:
        job = app.create_job(body.source, body.campaign_id)
        # inline-очередь выполняет pipeline синхронно — не держим HTTP-запрос
        background.add_task(_enqueue, app, job.id)
        return job.model_dump(mode="json")

    @api.get("/jobs/{job_id}", dependencies=protected)
    def get_job(job_id: str) -> dict[str, Any]:
        return app.job_summary(job_id)

    @api.post("/jobs/{job_id}/retry", status_code=202, dependencies=protected)
    def retry_job(
        job_id: str, background: BackgroundTasks, body: RetryRequest | None = None
    ) -> dict[str, str]:
        job = app.db.get_job(job_id)
        if job.status not in (JobStatus.failed, JobStatus.awaiting_review, JobStatus.queued):
            raise ValueError(f"job {job_id} is {job.status.value}; nothing to retry")
        if app.job_busy(job_id):
            raise JobBusy(f"job {job_id} is already queued or running")
        force = body.force_stage if body else None
        background.add_task(_enqueue, app, job_id, force)
        return {"job_id": job_id, "accepted": "true"}

    @api.post("/jobs/{job_id}/cancel", dependencies=protected)
    def cancel_job(job_id: str) -> dict[str, str]:
        return {"job_id": job_id, "result": app.cancel_job(job_id)}

    # ------------------------------------------------------------ clips and review

    def clip_view(job_id: str, clip_id: str) -> dict[str, Any]:
        clip = app.db.get_clip(job_id, clip_id)
        data = clip.model_dump(mode="json")
        data["meta"] = [
            m.model_dump(mode="json") for m in ReviewService(app).effective_meta(job_id, clip_id)
        ]
        data["video_url"] = f"/jobs/{job_id}/clips/{clip_id}/video"
        data["thumb_url"] = f"/jobs/{job_id}/clips/{clip_id}/thumb" if clip.thumb_key else None
        return data

    @api.get("/jobs/{job_id}/clips", dependencies=protected)
    def list_clips(job_id: str) -> list[dict[str, Any]]:
        app.db.get_job(job_id)
        return [clip_view(job_id, c.clip_id) for c in app.db.list_clips(job_id)]

    @api.get("/jobs/{job_id}/clips/{clip_id}", dependencies=protected)
    def get_clip(job_id: str, clip_id: str) -> dict[str, Any]:
        return clip_view(job_id, clip_id)

    @api.get("/jobs/{job_id}/clips/{clip_id}/video", dependencies=[Depends(media_auth)])
    def clip_video(job_id: str, clip_id: str) -> FileResponse:
        clip = app.db.get_clip(job_id, clip_id)
        if not clip.video_key:
            raise NotFound("clip has no video yet")
        return FileResponse(app.materialize(clip.video_key), media_type="video/mp4")

    @api.get("/jobs/{job_id}/clips/{clip_id}/thumb", dependencies=[Depends(media_auth)])
    def clip_thumb(job_id: str, clip_id: str) -> FileResponse:
        clip = app.db.get_clip(job_id, clip_id)
        path = app.materialize_optional(clip.thumb_key)
        if path is None:
            raise NotFound("clip has no thumbnail")
        return FileResponse(path, media_type="image/jpeg")

    def review(job_id: str) -> ReviewService:
        return ReviewService(app)

    @api.post("/jobs/{job_id}/clips/{clip_id}/approve", dependencies=protected)
    def approve(job_id: str, clip_id: str) -> dict[str, Any]:
        review(job_id).approve(job_id, clip_id, actor="api")
        return clip_view(job_id, clip_id)

    @api.post("/jobs/{job_id}/clips/{clip_id}/reject", dependencies=protected)
    def reject(job_id: str, clip_id: str, body: Reject | None = None) -> dict[str, Any]:
        review(job_id).reject(job_id, clip_id, actor="api", reason=body.reason if body else "")
        return clip_view(job_id, clip_id)

    @api.put("/jobs/{job_id}/clips/{clip_id}/metadata", dependencies=protected)
    def edit_metadata(job_id: str, clip_id: str, body: EditMetadata) -> dict[str, Any]:
        if body.title is None and body.description is None and body.hashtags is None:
            raise ValueError("nothing to change: pass title, description or hashtags")
        review(job_id).edit_metadata(
            job_id, clip_id, title=body.title, description=body.description,
            hashtags=body.hashtags, platform=body.platform, actor="api",
        )  # fmt: skip
        return clip_view(job_id, clip_id)

    @api.post(
        "/jobs/{job_id}/clips/{clip_id}/rerender-captions", status_code=202, dependencies=protected
    )
    def rerender_captions(job_id: str, clip_id: str, background: BackgroundTasks) -> dict[str, str]:
        review(job_id).rerender_captions(job_id, clip_id, actor="api", run=False)
        background.add_task(_enqueue, app, job_id, StageName.captions)
        return {"job_id": job_id, "accepted": "true"}

    @api.post(
        "/jobs/{job_id}/clips/{clip_id}/rerender-crop", status_code=202, dependencies=protected
    )
    def rerender_crop(
        job_id: str, clip_id: str, body: CropRequest, background: BackgroundTasks
    ) -> dict[str, str]:
        review(job_id).rerender_crop(
            job_id, clip_id, center_x=body.center_x, actor="api", run=False
        )
        background.add_task(_enqueue, app, job_id, StageName.reframe)
        return {"job_id": job_id, "accepted": "true"}

    # ------------------------------------------------------------ publishing and stats

    @api.post("/jobs/{job_id}/publish", dependencies=protected)
    def publish(job_id: str, schedule_only: bool = False) -> list[dict[str, Any]]:
        svc = PublishService(app)
        svc.schedule_job(job_id)
        if not schedule_only:
            svc.publish_job(job_id)
        return [p.model_dump(mode="json") for p in app.db.list_publications(job_id=job_id)]

    @api.get("/publications", dependencies=protected)
    def publications(job_id: str | None = None, account_id: str | None = None):
        pubs = app.db.list_publications(job_id=job_id, account_id=account_id)
        return [p.model_dump(mode="json") for p in pubs]

    @api.post("/publications/{publication_id}/stats", dependencies=protected)
    def manual_stats(publication_id: str, body: ManualStats) -> dict[str, Any]:
        snap = add_manual(
            app, publication_id, views=body.views, likes=body.likes, comments=body.comments
        )
        return snap.model_dump(mode="json")

    @api.get("/report", dependencies=protected)
    def report(campaign_id: str | None = None, top: int = Query(10, ge=1, le=100)):
        return build_report(app, campaign_id=campaign_id, top=top).model_dump(mode="json")

    return api


def _enqueue(app: Any, job_id: str, force_stage: StageName | None = None) -> None:
    """Фоновая постановка: ошибки пишутся в job/лог, а не теряются в ответе клиенту."""
    try:
        app.enqueue_job(job_id, force_stage=force_stage)
    except Exception as e:
        log.error("api.enqueue_failed", job_id=job_id, error=str(e)[:300])
        try:
            app.db.set_job_failed(job_id, None, type(e).__name__, str(e), True)
        except Exception:  # noqa: S110 — БД недоступна: остаётся только лог
            pass
