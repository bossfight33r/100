"""cf watch: новые видео каналов из config/watch.yaml сами идут в нарезку."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clipfactory.backends.downloader.discover import Lister
from clipfactory.discover import discover
from clipfactory.log import get_logger
from clipfactory.schemas import WatchSource

log = get_logger(__name__)


@dataclass
class WatchResult:
    source: WatchSource
    queued: list[tuple[str, str]] = field(default_factory=list)  # (job_id, url)
    error: str | None = None


def check_source(app: Any, src: WatchSource, lister: Lister) -> WatchResult:
    """Одна проверка источника. Ошибки — в результат: один сломанный канал не стопит остальные."""
    result = WatchResult(source=src)
    try:
        app.settings.campaign(src.campaign)
        found = discover(
            src.url,
            lister=lister,
            limit=src.limit,
            min_sec=src.min_minutes * 60,
            max_sec=src.max_minutes * 60,
            seen=app.db.job_sources(),
        )
        fresh = [c for c in found if (c.view_count or 0) >= src.min_views][: src.max_new]
        for cand in fresh:
            job = app.create_job(cand.url, src.campaign)
            result.queued.append((job.id, cand.url))
            try:
                app.enqueue_job(job.id)
            except Exception as e:  # InlineQueue выполняет сразу: падение job — не падение watch
                log.warning("watch.job_failed", job_id=job.id, error=str(e)[:200])
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"[:300]
        log.warning("watch.source_failed", url=src.url, error=result.error)
    return result


def check_all(app: Any, sources: list[WatchSource], lister: Lister) -> list[WatchResult]:
    return [check_source(app, s, lister) for s in sources]
