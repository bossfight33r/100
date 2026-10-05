from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from clipfactory.media import ffmpeg

FIXTURES = Path(__file__).parent / "fixtures"

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed"
)


def make_synthetic_video(
    path: Path,
    *,
    duration: float = 6.0,
    size: str = "640x360",
    fps: int = 25,
    audio: bool = True,
) -> Path:
    """testsrc2 + sine, H.264/AAC. Через media.ffmpeg, как и весь проект."""
    args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={duration}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    args.append(str(path))
    ffmpeg.ffmpeg(args, timeout=120)
    return path


@pytest.fixture(scope="session")
def synthetic_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    return make_synthetic_video(tmp_path_factory.mktemp("media") / "src.mp4", duration=6.0)


@pytest.fixture(scope="session")
def long_synthetic_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Видео с юникодом и пробелом в имени, достаточно длинное для нескольких клипов."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("media") / "Выпуск №1 тест.mp4"
    return make_synthetic_video(path, duration=75.0, size="640x360", fps=25)
