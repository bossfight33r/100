"""Поиск исходников: видео канала/плейлиста -> отфильтрованный и отсортированный список.

Сеть только через Lister/Prober из backends/downloader/discover.py.
"""

from __future__ import annotations

import math
import urllib.parse
from collections.abc import Iterable
from typing import Any

from clipfactory.backends.downloader.base import DownloadError
from clipfactory.backends.downloader.discover import Lister, Prober
from clipfactory.backends.downloader.ytdlp import source_info_from_ytdlp
from clipfactory.log import get_logger
from clipfactory.schemas import SourceCandidate

log = get_logger(__name__)

YOUTUBE_WATCH = "https://www.youtube.com/watch?v="


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f >= 0 else None


def entry_url(entry: dict[str, Any]) -> str | None:
    """URL видео из плоской записи; только http(s). У YouTube в flat-режиме бывает голый id."""
    for key in ("webpage_url", "url"):
        url = entry.get(key)
        if isinstance(url, str) and urllib.parse.urlparse(url).scheme in ("http", "https"):
            return url
    vid = entry.get("id")
    if isinstance(vid, str) and str(entry.get("ie_key", "")).lower() == "youtube":
        return YOUTUBE_WATCH + urllib.parse.quote(vid, safe="-_")
    return None


def to_candidate(entry: dict[str, Any], seen: set[str]) -> SourceCandidate | None:
    url = entry_url(entry)
    if url is None:
        return None
    if entry.get("live_status") in ("is_live", "is_upcoming"):
        return None  # идущий/будущий эфир не скачать целиком
    views = _num(entry.get("view_count"))
    return SourceCandidate(
        url=url,
        title=str(entry.get("title") or "")[:300],
        channel=str(entry.get("channel") or entry.get("uploader") or "")[:200],
        duration=_num(entry.get("duration")),
        view_count=int(views) if views is not None else None,
        already_processed=url in seen,
    )


def rank(
    candidates: Iterable[SourceCandidate],
    *,
    min_sec: float,
    max_sec: float,
    include_processed: bool = False,
) -> list[SourceCandidate]:
    """Фильтр по длительности (неизвестная — пропускается), сортировка по просмотрам."""
    out = []
    for c in candidates:
        if c.already_processed and not include_processed:
            continue
        if c.duration is not None and not (min_sec <= c.duration <= max_sec):
            continue
        out.append(c)
    return sorted(out, key=lambda c: (-(c.view_count or 0), c.title))


def discover(
    url: str,
    *,
    lister: Lister,
    prober: Prober | None = None,
    limit: int = 30,
    min_sec: float = 300,
    max_sec: float = 4 * 3600,
    seen: set[str] | None = None,
    include_processed: bool = False,
) -> list[SourceCandidate]:
    """prober задан — у каждого кандидата проверяется кривая «Most replayed» (запрос на видео)."""
    seen = seen or set()
    entries = lister(url, limit)
    candidates = [c for e in entries if (c := to_candidate(e, seen)) is not None]
    ranked = rank(candidates, min_sec=min_sec, max_sec=max_sec, include_processed=include_processed)
    if prober is None:
        return ranked
    checked = []
    for c in ranked:
        try:
            info = source_info_from_ytdlp(prober(c.url))
        except DownloadError as e:
            log.warning("discover.probe_failed", url=c.url, error=str(e)[:200])
            checked.append(c)
            continue
        update: dict[str, Any] = {"heatmap_points": len(info["heatmap"])}
        if c.duration is None and info["duration"]:
            update["duration"] = info["duration"]
        if c.view_count is None and info["view_count"] is not None:
            update["view_count"] = info["view_count"]
        c = c.model_copy(update=update)
        if c.duration is None or min_sec <= c.duration <= max_sec:
            checked.append(c)
    # с heatmap — выше: у них есть второй сигнал для selection: signals
    return sorted(checked, key=lambda c: (not c.heatmap_points, -(c.view_count or 0), c.title))
