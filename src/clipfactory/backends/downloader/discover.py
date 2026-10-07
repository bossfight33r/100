"""Список видео канала/плейлиста через yt-dlp — без скачивания (cf discover)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from clipfactory.backends.downloader.base import DownloadError

Lister = Callable[[str, int], list[dict[str, Any]]]
"""(url канала/плейлиста, максимум записей) -> плоские записи yt-dlp."""
Prober = Callable[[str], dict[str, Any]]
"""(url видео) -> полный info dict yt-dlp без скачивания (нужен для heatmap)."""


def list_videos_ytdlp(url: str, limit: int) -> list[dict[str, Any]]:  # pragma: no cover - сеть
    try:
        import yt_dlp
    except ImportError as e:
        raise DownloadError("yt-dlp is not installed") from e
    opts = {
        "extract_flat": "in_playlist",
        "playlistend": limit,
        "quiet": True,
        "noprogress": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise DownloadError(f"yt-dlp failed: {e}", retryable=True) from e
    return flatten_entries(info)[:limit]


def flatten_entries(info: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Канал отдаёт вкладки (Videos/Live/Shorts) как вложенные плейлисты — раскрываем."""
    if not isinstance(info, dict):
        return []
    entries = info.get("entries")
    if entries is None:
        return [info] if info.get("id") else []
    out: list[dict[str, Any]] = []
    for e in entries:
        if isinstance(e, dict) and e.get("entries") is not None:
            out.extend(flatten_entries(e))
        elif isinstance(e, dict):
            out.append(e)
    return out


def probe_video_ytdlp(url: str) -> dict[str, Any]:  # pragma: no cover - сеть
    try:
        import yt_dlp
    except ImportError as e:
        raise DownloadError("yt-dlp is not installed") from e
    opts = {"quiet": True, "noprogress": True, "skip_download": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception as e:
        raise DownloadError(f"yt-dlp failed: {e}", retryable=True) from e
