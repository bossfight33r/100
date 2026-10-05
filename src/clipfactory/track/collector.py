"""Сбор статистики: YouTube через API, остальные платформы — ручной ввод. Append-only."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from clipfactory.log import get_logger
from clipfactory.schemas import Platform, Publication, PublicationStatus, StatsSnapshot

log = get_logger(__name__)

TRACKABLE = [PublicationStatus.scheduled, PublicationStatus.published, PublicationStatus.exported]


class TrackError(Exception):
    pass


def fetch_youtube_stats(service: Any, video_ids: list[str]) -> dict[str, dict[str, int]]:
    """views/likes/comments по id видео, батчами по 50 (videos.list стоит 1 единицу квоты)."""
    out: dict[str, dict[str, int]] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = service.videos().list(part="statistics", id=",".join(batch), maxResults=50).execute()
        for item in resp.get("items", []):
            st = item.get("statistics", {})
            out[item["id"]] = {
                "views": int(st.get("viewCount", 0)),
                "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)),
            }
    return out


def default_youtube_service_factory(app: Any) -> Callable[[Any], Any]:  # pragma: no cover
    from clipfactory.publish.youtube import build_service

    return lambda account: build_service(account, app.settings.secrets_dir)


def collect_youtube(
    app: Any,
    service_factory: Callable[[Any], Any] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[StatsSnapshot]:
    from clipfactory.pipeline.publish import PublishService

    PublishService(app, publishers={}, clock=clock).mark_due_published()
    factory = service_factory or default_youtube_service_factory(app)
    by_account: dict[str, list[Publication]] = defaultdict(list)
    for pub in app.db.list_publications(statuses=[PublicationStatus.published]):
        if pub.platform == Platform.youtube and pub.external_id:
            by_account[pub.account_id].append(pub)
    snapshots = []
    now = clock()
    for account_id, pubs in by_account.items():
        account = app.settings.accounts.get(account_id)
        if account is None:
            log.warning("track.unknown_account", account_id=account_id)
            continue
        try:
            stats = fetch_youtube_stats(factory(account), [p.external_id for p in pubs])
        except Exception as e:  # одна ошибка аккаунта не останавливает остальные
            log.error("track.youtube_failed", account_id=account_id, error=str(e)[:300])
            continue
        for pub in pubs:
            st = stats.get(pub.external_id)
            if st is None:
                log.warning("track.video_missing", publication_id=pub.id)
                continue
            snap = StatsSnapshot(
                publication_id=pub.id, collected_at=now, source="youtube_api", **st
            )
            app.db.add_stats(snap)
            snapshots.append(snap)
    log.info("track.collected", snapshots=len(snapshots))
    return snapshots


def add_manual(
    app: Any,
    publication_id: str,
    *,
    views: int,
    likes: int = 0,
    comments: int = 0,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> StatsSnapshot:
    pub = app.db.get_publication(publication_id)
    if pub.status not in TRACKABLE:
        raise TrackError(f"publication {publication_id} is {pub.status.value}")
    history = app.db.stats_history(publication_id)
    if history and views < history[-1].views:
        log.warning("track.views_decreased", publication_id=publication_id)
    snap = StatsSnapshot(
        publication_id=publication_id, views=views, likes=likes, comments=comments,
        collected_at=clock(), source="manual",
    )  # fmt: skip
    app.db.add_stats(snap)
    return snap
