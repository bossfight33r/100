from __future__ import annotations


class X264Encoder:
    """Программный H.264 (libx264) — работает везде."""

    name = "x264"
    ffmpeg_encoder = "libx264"

    def __init__(self, preset: str = "medium", crf: int = 20) -> None:
        self.preset = preset
        self.crf = crf

    def video_args(self, *, fps: int) -> list[str]:
        return [
            "-c:v", self.ffmpeg_encoder,
            "-preset", self.preset,
            "-crf", str(self.crf),
            "-profile:v", "high",
            "-g", str(fps * 2),
            "-pix_fmt", "yuv420p",
        ]  # fmt: skip
