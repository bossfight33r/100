from __future__ import annotations


class VideoToolboxEncoder:
    """Аппаратный H.264 на macOS (Apple Silicon)."""

    name = "videotoolbox"
    ffmpeg_encoder = "h264_videotoolbox"

    def video_args(self, *, fps: int) -> list[str]:
        return [
            "-c:v", self.ffmpeg_encoder,
            "-b:v", "8M", "-maxrate", "12M", "-bufsize", "16M",
            "-profile:v", "high",
            "-allow_sw", "1",
            "-g", str(fps * 2),
            "-pix_fmt", "yuv420p",
        ]  # fmt: skip
