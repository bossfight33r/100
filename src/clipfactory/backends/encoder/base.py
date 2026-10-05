from __future__ import annotations

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
    preference: str, available_encoders: frozenset[str] | set[str]
) -> EncoderBackend:
    """Выбор по capability detection: videotoolbox только если реально есть в ffmpeg."""
    from clipfactory.backends.encoder.videotoolbox import VideoToolboxEncoder
    from clipfactory.backends.encoder.x264 import X264Encoder

    vt, x264 = VideoToolboxEncoder(), X264Encoder()
    if preference == "videotoolbox":
        if vt.ffmpeg_encoder not in available_encoders:
            raise EncoderUnavailable("h264_videotoolbox is not available in this ffmpeg build")
        return vt
    if preference == "x264":
        if x264.ffmpeg_encoder not in available_encoders:
            raise EncoderUnavailable("libx264 is not available in this ffmpeg build")
        return x264
    if vt.ffmpeg_encoder in available_encoders:
        return vt
    if x264.ffmpeg_encoder in available_encoders:
        return x264
    raise EncoderUnavailable("no H.264 encoder found (need libx264 or h264_videotoolbox)")
