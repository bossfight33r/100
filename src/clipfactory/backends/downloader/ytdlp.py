"""Загрузка по URL видеоплатформ через yt-dlp (только собственный/разрешённый контент)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from clipfactory.backends.downloader.base import DownloadError

SOURCE_INFO_NAME = "source.info.json"


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def source_info_from_ytdlp(info: dict[str, Any]) -> dict[str, Any]:
    """Безопасное подмножество info dict yt-dlp: название, кривая «Most replayed», главы.

    Битые точки отбрасываются: площадка может вернуть что угодно.
    """
    heatmap = []
    for p in info.get("heatmap") or []:
        if not isinstance(p, dict):
            continue
        s, e, v = _num(p.get("start_time")), _num(p.get("end_time")), _num(p.get("value"))
        if s is None or e is None or v is None or s < 0 or e <= s:
            continue
        heatmap.append({"start_time": s, "end_time": e, "value": min(max(v, 0.0), 1.0)})
    chapters = []
    for c in info.get("chapters") or []:
        if not isinstance(c, dict):
            continue
        s, e = _num(c.get("start_time")), _num(c.get("end_time"))
        if s is None or e is None or s < 0 or e <= s:
            continue
        chapters.append({"start_time": s, "end_time": e, "title": str(c.get("title") or "")[:200]})
    views = _num(info.get("view_count"))
    duration = _num(info.get("duration"))
    return {
        "title": str(info.get("title") or "")[:300],
        "channel": str(info.get("channel") or info.get("uploader") or "")[:200],
        "duration": duration if duration is not None and duration > 0 else None,
        "view_count": int(views) if views is not None and views >= 0 else None,
        "heatmap": heatmap,
        "chapters": chapters,
        "was_live": info.get("live_status") == "was_live" or info.get("was_live") is True,
    }


def download_ytdlp(url: str, dest_dir: Path) -> Path:  # pragma: no cover - сеть
    try:
        import yt_dlp
    except ImportError as e:
        raise DownloadError("yt-dlp is not installed") from e
    opts = {
        "outtmpl": str(dest_dir / "download.%(ext)s"),
        "format": "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/b[ext=mp4][height<=1080]/bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
    except Exception as e:
        raise DownloadError(f"yt-dlp failed: {e}", retryable=True) from e
    # рядом с видео: ingest заберёт в source.info.json (сигнал heatmap для select, ADR-0014)
    (dest_dir / SOURCE_INFO_NAME).write_text(
        json.dumps(source_info_from_ytdlp(info), ensure_ascii=False), encoding="utf-8"
    )
    if not path.exists():
        candidates = sorted(dest_dir.glob("download.*"))
        if not candidates:
            raise DownloadError("yt-dlp produced no file")
        path = candidates[0]
    return path
