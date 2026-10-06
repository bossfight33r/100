"""Планирование и публикация одобренных клипов. Пишет только в DB (publications).

Rejected и не прошедшие ревью клипы не публикуются никогда — проверка и при
планировании, и непосредственно перед загрузкой.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from clipfactory.log import get_logger
from clipfactory.pipeline.review import ReviewService
from clipfactory.publish.base import Publisher, PublishError, PublishRequest
from clipfactory.publish.scheduler import SchedulingError, plan_slots
from clipfactory.schemas import (
    Account,
    ClipStatus,
    JobStatus,
    Platform,
    Publication,
    PublicationStatus,
)

log = get_logger(__name__)

ACTIVE_PUB = [PublicationStatus.scheduled, PublicationStatus.publishing, PublicationStatus.published,
              PublicationStatus.exported]  # fmt: skip


class PublishServiceError(Exception):
    pass


def default_publishers(app: Any) -> dict[Platform, Publisher]:
    from clipfactory.publish.export import ExportPublisher
    from clipfactory.publish.youtube import YouTubePublisher, build_service

    exports = app.settings.data_dir / "exports"
    return {
        Platform.youtube: YouTubePublisher(
            lambda acc: build_service(acc, app.settings.secrets_dir)
        ),
        Platform.tiktok: ExportPublisher(Platform.tiktok, exports),
        Platform.instagram: ExportPublisher(Platform.instagram, exports),
    }


class PublishService:
    def __init__(
        self,
        app: Any,
        publishers: dict[Platform, Publisher] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.app = app
        self.db = app.db
        self._publishers = publishers
        self.clock = clock

    @property
    def publishers(self) -> dict[Platform, Publisher]:
        if self._publishers is None:
            self._publishers = default_publishers(self.app)
        return self._publishers

    def _accounts(self, campaign_id: str) -> list[Account]:
        campaign = self.app.settings.campaign(campaign_id)
        accounts = self.app.settings.accounts
        result = []
        for acc_id in campaign.accounts:
            acc = accounts.get(acc_id)
            if acc is None:
                raise PublishServiceError(
                    f"campaign {campaign_id} references unknown account {acc_id}"
                )
            if acc.platform in campaign.platforms:
                result.append(acc)
        if not result:
            raise PublishServiceError(f"campaign {campaign_id} has no accounts for its platforms")
        return result

    # ------------------------------------------------------------ schedule

    def schedule_job(self, job_id: str) -> list[Publication]:
        """Распределить одобренные клипы по слотам аккаунтов кампании. Идемпотентно."""
        job = self.db.get_job(job_id)
        if job.status not in (JobStatus.awaiting_review, JobStatus.scheduled, JobStatus.published):
            raise PublishServiceError(f"job {job_id} is {job.status.value}; review it first")
        clips = [c for c in self.db.list_clips(job_id) if c.status == ClipStatus.approved]
        if not clips:
            raise PublishServiceError(f"job {job_id} has no approved clips")
        now = self.clock()
        created: list[Publication] = []
        for acc in self._accounts(job.campaign_id):
            existing_pubs = self.db.list_publications(account_id=acc.id, statuses=ACTIVE_PUB)
            already = {(p.job_id, p.clip_id) for p in existing_pubs}
            todo = [
                c
                for c in sorted(clips, key=lambda c: -c.score)
                if (job_id, c.clip_id) not in already
            ]
            if not todo:
                continue
            slots = plan_slots(
                acc,
                len(todo),
                now=now,
                existing=[p.scheduled_at for p in existing_pubs if p.scheduled_at],
            )
            for clip, slot in zip(todo, slots, strict=True):
                pub = Publication(
                    id=f"{job_id}-{clip.clip_id}-{acc.id}",
                    job_id=job_id,
                    clip_id=clip.clip_id,
                    campaign_id=job.campaign_id,
                    platform=acc.platform,
                    account_id=acc.id,
                    scheduled_at=slot,
                    status=PublicationStatus.scheduled,
                )
                self.db.save_publication(pub)
                created.append(pub)
                log.info("publish.scheduled", publication_id=pub.id, at=slot.isoformat())
        # уже полностью опубликованный job не должен откатываться в scheduled
        self._refresh_job_status(job_id)
        return created

    # ------------------------------------------------------------ publish

    def publish_job(self, job_id: str) -> list[Publication]:
        """Загрузить/экспортировать все запланированные публикации job."""
        pubs = [
            p
            for p in self.db.list_publications(job_id=job_id)
            if p.status == PublicationStatus.scheduled and p.external_id is None
        ]
        if not pubs:
            return []
        self.db.set_job_status(job_id, JobStatus.publishing)
        review = ReviewService(self.app)
        accounts = self.app.settings.accounts
        done = []
        for pub in pubs:
            clip = self.db.get_clip(pub.job_id, pub.clip_id)
            if clip.status != ClipStatus.approved:
                pub.status, pub.error = PublicationStatus.failed, f"clip is {clip.status.value}"
                self.db.save_publication(pub)
                log.warning("publish.blocked_not_approved", publication_id=pub.id)
                continue
            if pub.scheduled_at is not None and pub.scheduled_at <= self.clock():
                # слот прошёл (повтор после квоты/сети) — без перепланирования YouTube
                # опубликовал бы сразу, мимо окон, daily_limit и интервала
                try:
                    pub.scheduled_at = self._replan(pub)
                except SchedulingError as e:
                    pub.status, pub.error = PublicationStatus.scheduled, str(e)[:500]
                    self.db.save_publication(pub)
                    log.error("publish.replan_failed", publication_id=pub.id)
                    continue
                self.db.save_publication(pub)
                log.info(
                    "publish.rescheduled", publication_id=pub.id, at=pub.scheduled_at.isoformat()
                )
            metas = {m.platform: m for m in review.effective_meta(pub.job_id, pub.clip_id)}
            meta = metas.get(pub.platform) or next(iter(metas.values()))
            if meta.platform != pub.platform:
                meta = meta.model_copy(update={"platform": pub.platform})
            req = PublishRequest(
                publication_id=pub.id,
                account=accounts[pub.account_id],
                video_path=self.app.storage.local_path(clip.video_key),
                thumb_path=self.app.storage.local_path(clip.thumb_key) if clip.thumb_key else None,
                meta=meta,
                scheduled_at=pub.scheduled_at,
            )
            pub.status = PublicationStatus.publishing
            self.db.save_publication(pub)
            try:
                result = self.publishers[pub.platform].publish(req)
            except PublishError as e:
                # retryable (квота, сеть) -> вернуть в scheduled, иначе failed
                pub.status = (
                    PublicationStatus.scheduled if e.retryable else PublicationStatus.failed
                )
                pub.error = str(e)[:500]
                self.db.save_publication(pub)
                log.error("publish.failed", publication_id=pub.id, retryable=e.retryable)
                continue
            pub.status, pub.external_id, pub.url, pub.error = (
                result.status, result.external_id, result.url, None,
            )  # fmt: skip
            if result.status == PublicationStatus.published:
                pub.published_at = self.clock()
            self.db.save_publication(pub)
            done.append(pub)
            log.info("publish.done", publication_id=pub.id, status=result.status.value)
        self._refresh_job_status(job_id)
        return done

    def _replan(self, pub: Publication) -> datetime:
        account = self.app.settings.accounts[pub.account_id]
        existing = [
            p.scheduled_at
            for p in self.db.list_publications(account_id=pub.account_id, statuses=ACTIVE_PUB)
            if p.id != pub.id and p.scheduled_at
        ]
        return plan_slots(account, 1, now=self.clock(), existing=existing)[0]

    def mark_due_published(self) -> int:
        """YouTube публикует сам по publishAt; отмечаем такие публикации как published."""
        now = self.clock()
        n = 0
        for pub in self.db.list_publications(statuses=[PublicationStatus.scheduled]):
            if pub.external_id and pub.scheduled_at and pub.scheduled_at <= now:
                pub.status, pub.published_at = PublicationStatus.published, pub.scheduled_at
                self.db.save_publication(pub)
                self._refresh_job_status(pub.job_id)
                n += 1
        return n

    def _refresh_job_status(self, job_id: str) -> None:
        pubs = self.db.list_publications(job_id=job_id)
        if not pubs:
            return
        final = (PublicationStatus.published, PublicationStatus.exported, PublicationStatus.failed)
        if all(p.status in final for p in pubs):
            self.db.set_job_status(job_id, JobStatus.published)
        else:
            self.db.set_job_status(job_id, JobStatus.scheduled)
