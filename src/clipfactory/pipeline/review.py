"""Ревью клипов: approve / reject / edit metadata / rerender captions / rerender crop.

Все действия пишутся в review_actions. Перерендер — через overrides.json (ввод
пользователя) и повторный запуск job: кеш пересчитает только затронутые этапы.
"""

from __future__ import annotations

from typing import Any

from clipfactory.db import NotFound
from clipfactory.log import get_logger
from clipfactory.schemas import (
    ClipMeta,
    ClipRecord,
    ClipStatus,
    JobStatus,
    Platform,
    PlatformClipMeta,
    ReviewAction,
    ReviewActionType,
    ReviewOverrides,
    StageName,
)

log = get_logger(__name__)

REVIEWABLE = (JobStatus.awaiting_review, JobStatus.scheduled)
RERENDERABLE = (JobStatus.awaiting_review, JobStatus.failed)


class ReviewError(Exception):
    pass


class ReviewService:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.db = app.db

    # ------------------------------------------------------------ helpers

    def _clip(self, job_id: str, clip_id: str) -> ClipRecord:
        try:
            return self.db.get_clip(job_id, clip_id)
        except NotFound as e:
            raise ReviewError(str(e)) from e

    def _log(self, job_id: str, clip_id: str, action: ReviewActionType, actor: str, **payload: Any):
        self.db.add_review_action(
            ReviewAction(
                job_id=job_id, clip_id=clip_id, action=action, payload=payload, actor=actor
            )
        )
        log.info("review.action", job_id=job_id, clip_id=clip_id, action=action.value, actor=actor)

    def _require_status(self, job_id: str, allowed: tuple[JobStatus, ...]) -> None:
        status = self.db.get_job(job_id).status
        if status not in allowed:
            raise ReviewError(f"job {job_id} is {status.value}; action not allowed")

    def effective_meta(self, job_id: str, clip_id: str) -> list[PlatformClipMeta]:
        """Метаданные для публикации: правка ревью важнее сгенерированных."""
        clip = self._clip(job_id, clip_id)
        if clip.meta_override:
            return clip.meta_override
        if not clip.meta_key or not self.app.storage.exists(clip.meta_key):
            raise ReviewError(f"clip {clip_id} has no meta.json")
        with self.app.storage.open_read(clip.meta_key) as f:
            return ClipMeta.model_validate_json(f.read()).platforms

    # ------------------------------------------------------------ actions

    def approve(self, job_id: str, clip_id: str, actor: str = "cli") -> ClipRecord:
        self._require_status(job_id, REVIEWABLE)
        self._clip(job_id, clip_id)
        self.db.set_clip_status(job_id, clip_id, ClipStatus.approved)
        self._log(job_id, clip_id, ReviewActionType.approve, actor)
        return self._clip(job_id, clip_id)

    def reject(self, job_id: str, clip_id: str, actor: str = "cli", reason: str = "") -> ClipRecord:
        self._require_status(job_id, REVIEWABLE)
        self._clip(job_id, clip_id)
        self.db.set_clip_status(job_id, clip_id, ClipStatus.rejected)
        self._log(job_id, clip_id, ReviewActionType.reject, actor, reason=reason)
        return self._clip(job_id, clip_id)

    def edit_metadata(
        self,
        job_id: str,
        clip_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        hashtags: list[str] | None = None,
        platform: Platform | None = None,
        actor: str = "cli",
    ) -> list[PlatformClipMeta]:
        """Правка для одной платформы или всех. Правила кампании применяются снова."""
        from clipfactory.pipeline.render import enforce_campaign_rules
        from clipfactory.schemas import ClipCandidate

        self._require_status(job_id, REVIEWABLE)
        clip = self._clip(job_id, clip_id)
        campaign = self.app.settings.campaign(self.db.get_job(job_id).campaign_id)
        cand = ClipCandidate(
            id=clip_id, start=clip.start, end=clip.end, score=clip.score, hook=clip.hook
        )
        current = self.effective_meta(job_id, clip_id)
        updated = []
        for meta in current:
            if platform is not None and meta.platform != platform:
                updated.append(meta)
                continue
            changed = meta.model_copy(
                update={
                    k: v
                    for k, v in {
                        "title": title,
                        "description": description,
                        "hashtags": hashtags,
                    }.items()
                    if v is not None
                }
            )
            updated.append(enforce_campaign_rules(changed, campaign, cand))
        self.db.set_clip_meta_override(job_id, clip_id, updated)
        self._log(
            job_id, clip_id, ReviewActionType.edit_metadata, actor,
            platform=platform.value if platform else None,
            title=title, description=description, hashtags=hashtags,
        )  # fmt: skip
        return updated

    def _require_rerenderable(self, job_id: str, clip_id: str) -> None:
        """Проверить всё до изменений: иначе JobBusy оставил бы полуприменённую правку."""
        self._require_status(job_id, RERENDERABLE)
        self._clip(job_id, clip_id)
        if self.app.job_busy(job_id):
            raise ReviewError(f"job {job_id} is already queued or running; try again later")

    def _save_overrides(self, job_id: str, overrides: ReviewOverrides) -> None:
        self.app.storage.put_bytes(
            self.app.overrides_key(job_id), overrides.model_dump_json(indent=2).encode()
        )

    def rerender_captions(
        self, job_id: str, clip_id: str, actor: str = "cli", *, run: bool = True
    ) -> StageName:
        """Подготовить перерендер субтитров; run=False — запуск job оставить вызывающему."""
        self._require_rerenderable(job_id, clip_id)
        ov = self.app.load_overrides(job_id)
        ov.caption_nonce[clip_id] = ov.caption_nonce.get(clip_id, 0) + 1
        self._save_overrides(job_id, ov)
        self.db.set_clip_status(job_id, clip_id, ClipStatus.pending_review)
        self._log(job_id, clip_id, ReviewActionType.rerender_captions, actor)
        if run:
            self.app.enqueue_job(job_id, force_stage=StageName.captions)
        return StageName.captions

    def rerender_crop(
        self,
        job_id: str,
        clip_id: str,
        center_x: float | None = None,
        actor: str = "cli",
        *,
        run: bool = True,
    ) -> StageName:
        """center_x 0..1 — ручной центр кропа; None — снять ручную правку и пересчитать."""
        self._require_rerenderable(job_id, clip_id)
        if center_x is not None and not 0.0 <= center_x <= 1.0:
            raise ReviewError("center_x must be between 0 and 1")
        ov = self.app.load_overrides(job_id)
        if center_x is None:
            ov.crop_center_x.pop(clip_id, None)
        else:
            ov.crop_center_x[clip_id] = round(center_x, 3)
        self._save_overrides(job_id, ov)
        self.db.set_clip_status(job_id, clip_id, ClipStatus.pending_review)
        self._log(job_id, clip_id, ReviewActionType.rerender_crop, actor, center_x=center_x)
        if run:
            self.app.enqueue_job(job_id, force_stage=StageName.reframe)
        return StageName.reframe
