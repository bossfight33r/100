"""ingest: локальный файл / HTTP(S) / yt-dlp URL -> jobs/{id}/source.mp4."""

from __future__ import annotations

import shutil
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipfactory.log import get_logger
from clipfactory.media import ffmpeg
from clipfactory.media.models import MediaInfo
from clipfactory.media.probe import ProbeError, probe
from clipfactory.pipeline.context import SourceError, StageContext, ValidationFailed
from clipfactory.schemas import StageName, StageResult
from clipfactory.storage.local import sha256_file

log = get_logger(__name__)

DIRECT_MEDIA_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
MP4_FAMILY = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
MP4_VIDEO_CODECS = {"h264", "hevc", "av1", "mpeg4"}
MP4_AUDIO_CODECS = {"aac", "mp3", "alac", "ac3", "eac3", "opus"}

Downloader = Callable[[str, Path], Path]


def is_url(source: str) -> bool:
    return urllib.parse.urlparse(source).scheme in ("http", "https")


def download_http(url: str, dest_dir: Path) -> Path:
    """Прямая загрузка медиафайла по HTTP(S) потоком."""
    name = Path(urllib.parse.urlparse(url).path).name or "download.bin"
    dest = dest_dir / name
    req = urllib.request.Request(url, headers={"User-Agent": "clipfactory/0.1"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as f:  # noqa: S310
            shutil.copyfileobj(resp, f, length=1024 * 1024)
    except OSError as e:
        raise SourceError(f"download failed: {e}", retryable=True) from e
    return dest


def download_ytdlp(url: str, dest_dir: Path) -> Path:  # pragma: no cover - сеть
    try:
        import yt_dlp
    except ImportError as e:
        raise SourceError("yt-dlp is not installed") from e
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
        raise SourceError(f"yt-dlp failed: {e}", retryable=True) from e
    if not path.exists():
        candidates = sorted(dest_dir.glob("download.*"))
        if not candidates:
            raise SourceError("yt-dlp produced no file")
        path = candidates[0]
    return path


def needs_remux(info: MediaInfo) -> bool:
    fmt = set(info.format_name.split(","))
    if not fmt & MP4_FAMILY:
        return True
    if info.video and info.video.codec not in MP4_VIDEO_CODECS:
        return True
    return bool(info.audio and info.audio.codec not in MP4_AUDIO_CODECS)


class IngestStage:
    name = StageName.ingest
    version = 1

    def __init__(
        self,
        http_downloader: Downloader = download_http,
        ytdlp_downloader: Downloader = download_ytdlp,
    ) -> None:
        self.http_downloader = http_downloader
        self.ytdlp_downloader = ytdlp_downloader

    def config(self, ctx: StageContext) -> dict[str, Any]:
        return {"source": ctx.job.source}

    def input_keys(self, ctx: StageContext) -> list[str]:
        return []

    def source_fingerprint(self, ctx: StageContext) -> str:
        """Отпечаток внешнего входа: sha256 локального файла или сам URL."""
        src = ctx.job.source
        if is_url(src):
            return f"url:{src}"
        path = Path(src)
        if not path.is_file():
            raise SourceError(f"source file not found: {src}")
        return f"sha256:{sha256_file(path)}"

    def _fetch(self, ctx: StageContext) -> Path:
        src = ctx.job.source
        if not is_url(src):
            path = Path(src).expanduser()
            if not path.is_file():
                raise SourceError(f"source file not found: {src}")
            return path
        dl_dir = ctx.scratch / "download"
        dl_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(urllib.parse.urlparse(src).path).suffix.lower()
        if ext in DIRECT_MEDIA_EXT:
            return self.http_downloader(src, dl_dir)
        return self.ytdlp_downloader(src, dl_dir)

    def run(self, ctx: StageContext) -> StageResult:
        fetched = self._fetch(ctx)
        try:
            info = probe(fetched)
        except ProbeError as e:
            raise SourceError(f"source is not a readable media file: {e}") from e
        if info.video is None:
            raise SourceError("source has no video stream")

        out_key = ctx.key("source.mp4")
        out_path = ctx.local_path(out_key)
        remuxed = False
        if needs_remux(info):
            remuxed = True
            tmp = out_path.with_name(".source.remux.mp4")
            log_path = ctx.local_path(ctx.log_key(self.name))
            try:
                ffmpeg.ffmpeg(
                    ["-i", str(fetched), "-map", "0:v:0", "-map", "0:a:0?",
                     "-c", "copy", "-movflags", "+faststart", str(tmp)],
                    log_path=log_path, cancel=ctx.cancel, timeout=ctx.settings.ffmpeg_timeout_sec,
                )  # fmt: skip
            except ffmpeg.FFmpegError:
                log.info("ingest.remux_failed_transcoding", job_id=ctx.job.id)
                ffmpeg.ffmpeg(
                    ["-i", str(fetched), "-map", "0:v:0", "-map", "0:a:0?",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                     "-movflags", "+faststart", str(tmp)],
                    log_path=log_path, cancel=ctx.cancel, timeout=ctx.settings.ffmpeg_timeout_sec,
                )  # fmt: skip
            ctx.storage.put_file(out_key, tmp)
            tmp.unlink(missing_ok=True)
        else:
            ctx.storage.put_file(out_key, fetched)

        return StageResult(
            stage=self.name,
            outputs=[out_key],
            info={
                # без перекодирования source.mp4 — копия источника: его хеш LocalStorage
                # кеширует и переиспользует для манифеста, повторного чтения файла нет
                "source_checksum": None if remuxed else ctx.storage.checksum(out_key),
                "remuxed": remuxed,
                "duration": info.duration,
                "width": info.display_size[0],
                "height": info.display_size[1],
                "has_audio": info.audio is not None,
            },
        )

    def validate(self, ctx: StageContext, outputs: list[str]) -> None:
        path = ctx.local_path(ctx.key("source.mp4"))
        try:
            info = probe(path)
        except ProbeError as e:
            raise ValidationFailed(f"source.mp4 is unreadable: {e}") from e
        if info.video is None or info.duration <= 0:
            raise ValidationFailed("source.mp4 has no video or zero duration")
