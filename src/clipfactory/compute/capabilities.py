"""Что умеет текущая машина. Pipeline описывает требования, а не платформу."""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil

from pydantic import BaseModel, Field

from clipfactory.media import ffmpeg


class WorkerCapabilities(BaseModel):
    os: str
    arch: str
    cpu_threads: int
    ram_mb: int
    has_ffmpeg: bool
    has_ffprobe: bool
    ffmpeg_version: str | None = None
    has_videotoolbox: bool
    available_encoders: list[str] = Field(default_factory=list)
    available_transcribers: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class TaskRequirements(BaseModel):
    """Требования задачи к воркеру. Сегодня всё выполняется локально; контракт под routing."""

    transcriber: str | None = None
    min_ram_mb: int = 0
    encoder: str | None = None  # "any_h264" | конкретный ffmpeg-энкодер
    tags: list[str] = Field(default_factory=list)


H264_ENCODERS = ("h264_videotoolbox", "libx264")


def _ram_mb() -> int:
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 * 1024))
    except (ValueError, OSError, AttributeError):  # pragma: no cover
        return 0


def detect_transcribers() -> list[str]:
    from clipfactory.backends.transcriber.mlx import mlx_available

    found = ["fake"]
    if importlib.util.find_spec("faster_whisper") is not None:
        found.append("faster_whisper")
    if mlx_available():
        found.append("mlx")
    return found


def detect() -> WorkerCapabilities:
    has_ffmpeg = shutil.which("ffmpeg") is not None
    encoders: list[str] = []
    version = None
    if has_ffmpeg:
        all_enc = ffmpeg.list_encoders()
        encoders = sorted(
            e for e in all_enc if "264" in e or "265" in e or "hevc" in e or e == "aac"
        )
        version = ffmpeg.version()
    tags = [f"os:{platform.system().lower()}", f"arch:{platform.machine().lower()}"]
    return WorkerCapabilities(
        os=platform.system(),
        arch=platform.machine(),
        cpu_threads=os.cpu_count() or 1,
        ram_mb=_ram_mb(),
        has_ffmpeg=has_ffmpeg,
        has_ffprobe=shutil.which("ffprobe") is not None,
        ffmpeg_version=version,
        has_videotoolbox="h264_videotoolbox" in encoders,
        available_encoders=encoders,
        available_transcribers=detect_transcribers(),
        tags=tags,
    )


def satisfies(caps: WorkerCapabilities, req: TaskRequirements) -> list[str]:
    """Пустой список — воркер подходит; иначе причины отказа."""
    problems = []
    if req.min_ram_mb and caps.ram_mb and caps.ram_mb < req.min_ram_mb:
        problems.append(f"ram {caps.ram_mb}MB < {req.min_ram_mb}MB")
    if req.transcriber and req.transcriber != "any":
        if req.transcriber not in caps.available_transcribers:
            problems.append(f"transcriber {req.transcriber} unavailable")
    if req.encoder == "any_h264":
        if not any(e in caps.available_encoders for e in H264_ENCODERS):
            problems.append("no H.264 encoder")
    elif req.encoder and req.encoder not in caps.available_encoders:
        problems.append(f"encoder {req.encoder} unavailable")
    missing_tags = set(req.tags) - set(caps.tags)
    if missing_tags:
        problems.append(f"missing tags {sorted(missing_tags)}")
    return problems
