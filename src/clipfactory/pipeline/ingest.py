"""ingest: локальный файл / HTTP(S) / yt-dlp URL -> jobs/{id}/source.mp4."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from clipfactory.backends.downloader import (
    SOURCE_INFO_NAME,
    Downloader,
    DownloadError,
    download_http,
    download_ytdlp,
)
from clipfactory.backends.downloader.live_chat import (
    ChatFetcher,
    fetch_live_chat_ytdlp,
    parse_live_chat,
)
from clipfactory.log import get_logger
from clipfactory.media import ffmpeg
from clipfactory.media.models import MediaInfo
from clipfactory.media.probe import ProbeError, probe
from clipfactory.pipeline.context import SourceError, StageContext, ValidationFailed
from clipfactory.schemas import ChatActivity, SelectionMode, SourceInfo, StageName, StageResult
from clipfactory.storage.local import sha256_file

log = get_logger(__name__)

PLAYLIST_FORMATS = {"hls", "applehttp", "concat", "ffconcat", "dash"}
CHAT_HOP = 1.0
DIRECT_MEDIA_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
MP4_FAMILY = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
MP4_VIDEO_CODECS = {"h264", "hevc", "av1", "mpeg4"}
MP4_AUDIO_CODECS = {"aac", "mp3", "alac", "ac3", "eac3", "opus"}


def is_url(source: str) -> bool:
    return urllib.parse.urlparse(source).scheme in ("http", "https")


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
        chat_fetcher: ChatFetcher = fetch_live_chat_ytdlp,
    ) -> None:
        self.http_downloader = http_downloader
        self.ytdlp_downloader = ytdlp_downloader
        self.chat_fetcher = chat_fetcher

    @staticmethod
    def wants_chat(ctx: StageContext) -> bool:
        return ctx.campaign.selection is SelectionMode.signals

    def config(self, ctx: StageContext) -> dict[str, Any]:
        cfg: dict[str, Any] = {"source": ctx.job.source}
        if self.wants_chat(ctx):  # ключ только для signals: кеш прежних job не меняется
            cfg["chat"] = True
        return cfg

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
            downloader = self.http_downloader
        else:
            downloader = self.ytdlp_downloader
        try:
            return downloader(src, dl_dir)
        except DownloadError as e:
            raise SourceError(str(e), retryable=e.retryable) from e

    def run(self, ctx: StageContext) -> StageResult:
        fetched = self._fetch(ctx)
        try:
            info = probe(fetched)
        except ProbeError as e:
            raise SourceError(f"source is not a readable media file: {e}") from e
        if set(info.format_name.split(",")) & PLAYLIST_FORMATS:
            # плейлист вместо медиа: ffmpeg читал бы файлы/URL из его содержимого
            raise SourceError(f"playlist sources are not allowed ({info.format_name})")
        if info.video is None:
            raise SourceError("source has no video stream")

        out_key = ctx.key("source.mp4")
        out_path = ctx.local_path(out_key)
        remuxed = False
        if needs_remux(info):
            remuxed = True
            tmp = out_path.with_name(".source.remux.mp4")
            log_path = ctx.log_path(self.name)
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

        outputs = [out_key]
        source_info = self._store_source_info(ctx)
        if source_info is not None:
            outputs.append(ctx.key("source.info.json"))
        chat_key = self._store_chat(ctx, source_info, info.duration)
        if chat_key:
            outputs.append(chat_key)
        return StageResult(
            stage=self.name,
            outputs=outputs,
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

    def _store_source_info(self, ctx: StageContext) -> SourceInfo | None:
        """source.info.json от загрузчика (heatmap, главы) — необязательный выход."""
        raw = ctx.scratch / "download" / SOURCE_INFO_NAME
        key = ctx.key("source.info.json")
        ctx.storage.delete(key)  # не оставлять кривую от прошлой загрузки
        if not is_url(ctx.job.source) or not raw.is_file():
            return None
        try:
            info = SourceInfo.model_validate_json(raw.read_bytes())
        except ValueError as e:
            log.warning("ingest.source_info_invalid", job_id=ctx.job.id, error=str(e)[:200])
            return None
        ctx.write_model(key, info)
        log.info("ingest.source_info", job_id=ctx.job.id, heatmap_points=len(info.heatmap))
        return info

    def _store_chat(
        self, ctx: StageContext, info: SourceInfo | None, duration: float
    ) -> str | None:
        """Чат записи стрима — лучшее усилие: без него ingest всё равно успешен."""
        key = ctx.key("chat.json")
        ctx.storage.delete(key)
        if info is None or not info.was_live or not self.wants_chat(ctx):
            return None
        chat_dir = ctx.scratch / "chat"
        chat_dir.mkdir(parents=True, exist_ok=True)
        try:
            path = self.chat_fetcher(ctx.job.source, chat_dir)
        except DownloadError as e:
            log.warning("ingest.chat_failed", job_id=ctx.job.id, error=str(e)[:200])
            return None
        if path is None:
            return None
        with path.open(encoding="utf-8", errors="replace") as f:
            counts = parse_live_chat(f, duration, hop=CHAT_HOP)
        if not any(counts):
            return None
        ctx.write_model(key, ChatActivity(hop=CHAT_HOP, counts=counts))
        log.info("ingest.chat", job_id=ctx.job.id, messages=sum(counts))
        return key

    def validate(self, ctx: StageContext, outputs: list[str]) -> None:
        path = ctx.local_path(ctx.key("source.mp4"))
        try:
            info = probe(path)
        except ProbeError as e:
            raise ValidationFailed(f"source.mp4 is unreadable: {e}") from e
        if info.video is None or info.duration <= 0:
            raise ValidationFailed("source.mp4 has no video or zero duration")
