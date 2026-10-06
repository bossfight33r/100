from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class EncoderBackend(Protocol):
    """H.264-энкодер для финального рендера."""

    name: str
    ffmpeg_encoder: str

    def video_args(self, *, fps: int) -> list[str]:
        """Аргументы ffmpeg для видеопотока (-c:v ... и параметры качества)."""
        ...


class EncoderUnavailable(Exception):
    pass


def select_encoder(
    preference: str,
    available_encoders: frozenset[str] | set[str],
    works: Callable[[str], bool] = lambda name: True,
) -> EncoderBackend:
    """Выбор по capability detection: энкодер должен быть в ffmpeg И реально кодировать.

    ``works`` — пробное кодирование (ffmpeg перечисляет h264_nvenc и без GPU).
    auto: videotoolbox -> nvenc -> x264.
    """
    from clipfactory.backends.encoder.nvenc import NvencEncoder
    from clipfactory.backends.encoder.videotoolbox import VideoToolboxEncoder
    from clipfactory.backends.encoder.x264 import X264Encoder

    candidates: dict[str, EncoderBackend] = {
        "videotoolbox": VideoToolboxEncoder(),
        "nvenc": NvencEncoder(),
        "x264": X264Encoder(),
    }

    def usable(enc: EncoderBackend) -> bool:
        return enc.ffmpeg_encoder in available_encoders and works(enc.ffmpeg_encoder)

    if preference != "auto":
        enc = candidates.get(preference)
        if enc is None:
            raise EncoderUnavailable(f"unknown encoder {preference!r}")
        if not usable(enc):
            raise EncoderUnavailable(f"{enc.ffmpeg_encoder} is not usable on this machine")
        return enc
    for enc in candidates.values():
        if usable(enc):
            return enc
    raise EncoderUnavailable(
        "no working H.264 encoder (need libx264, h264_videotoolbox or h264_nvenc)"
    )
