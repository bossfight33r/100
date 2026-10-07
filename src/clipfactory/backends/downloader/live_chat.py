"""Чат записи YouTube-стрима (live chat replay) через yt-dlp — сигнал для select (ADR-0016)."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable
from pathlib import Path

from clipfactory.backends.downloader.base import DownloadError

ChatFetcher = Callable[[str, Path], Path | None]
"""(url видео, каталог) -> путь к .live_chat.json (JSON lines) или None, если чата нет."""


def fetch_live_chat_ytdlp(url: str, dest_dir: Path) -> Path | None:  # pragma: no cover - сеть
    try:
        import yt_dlp
    except ImportError as e:
        raise DownloadError("yt-dlp is not installed") from e
    opts = {
        "outtmpl": str(dest_dir / "chat.%(ext)s"),
        "skip_download": True,
        "writesubtitles": True,
        "subtitleslangs": ["live_chat"],
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as e:
        raise DownloadError(f"live chat download failed: {e}", retryable=True) from e
    found = sorted(dest_dir.glob("chat*.live_chat.json"))
    return found[0] if found else None


def parse_live_chat(lines: Iterable[str], duration: float, hop: float = 1.0) -> list[int]:
    """Число сообщений на каждые hop секунд. Битые строки пропускаются."""
    n = max(int(math.ceil(duration / hop)), 1)
    counts = [0] * n
    for line in lines:
        try:
            item = json.loads(line)
            replay = item["replayChatItemAction"]
            offset = float(replay["videoOffsetTimeMsec"]) / 1000.0
            actions = replay.get("actions") or []
        except (ValueError, KeyError, TypeError):
            continue
        if not math.isfinite(offset) or offset < 0:
            continue
        messages = sum(1 for a in actions if isinstance(a, dict) and "addChatItemAction" in a)
        idx = int(offset / hop)
        if messages and idx < n:
            counts[idx] += messages
    return counts
