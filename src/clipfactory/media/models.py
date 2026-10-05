from __future__ import annotations

from pydantic import BaseModel


class VideoStream(BaseModel):
    codec: str
    width: int
    height: int
    fps: float
    pix_fmt: str | None = None
    rotation: int = 0


class AudioStream(BaseModel):
    codec: str
    sample_rate: int
    channels: int


class MediaInfo(BaseModel):
    format_name: str
    duration: float
    size: int | None = None
    video: VideoStream | None = None
    audio: AudioStream | None = None

    @property
    def display_size(self) -> tuple[int, int]:
        """Размер кадра с учётом поворота (телефонные видео)."""
        if self.video is None:
            return (0, 0)
        w, h = self.video.width, self.video.height
        if self.video.rotation % 180 == 90:
            return (h, w)
        return (w, h)
