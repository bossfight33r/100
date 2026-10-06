"""Загрузка по URL видеоплатформ через yt-dlp (только собственный/разрешённый контент)."""

from __future__ import annotations

from pathlib import Path

from clipfactory.backends.downloader.base import DownloadError


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
    if not path.exists():
        candidates = sorted(dest_dir.glob("download.*"))
        if not candidates:
            raise DownloadError("yt-dlp produced no file")
        path = candidates[0]
    return path
