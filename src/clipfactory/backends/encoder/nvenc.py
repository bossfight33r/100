from __future__ import annotations


class NvencEncoder:
    """Аппаратный H.264 на NVIDIA (GPU-сервер)."""

    name = "nvenc"
    ffmpeg_encoder = "h264_nvenc"

    def video_args(self, *, fps: int) -> list[str]:
        return [
            "-c:v", self.ffmpeg_encoder,
            "-preset", "p5", "-tune", "hq",
            "-rc", "vbr", "-cq", "21", "-b:v", "0",
            "-profile:v", "high",
            "-g", str(fps * 2),
            "-pix_fmt", "yuv420p",
        ]  # fmt: skip
