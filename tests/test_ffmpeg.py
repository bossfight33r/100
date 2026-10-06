import json
import threading
import time

import pytest

from clipfactory.backends.encoder import EncoderUnavailable, select_encoder
from clipfactory.compute.capabilities import TaskRequirements, WorkerCapabilities, satisfies
from clipfactory.media import ffmpeg
from clipfactory.media.probe import parse_probe, probe
from tests.conftest import FIXTURES, needs_ffmpeg


@needs_ffmpeg
def test_run_failure_has_tail_and_log(tmp_path):
    log = tmp_path / "logs" / "fail.log"
    with pytest.raises(ffmpeg.FFmpegError) as ei:
        ffmpeg.ffmpeg(["-i", str(tmp_path / "missing.mp4"), "-f", "null", "-"], log_path=log)
    err = ei.value
    assert err.returncode not in (None, 0)
    assert "missing.mp4" in err.stderr_tail
    assert len(err.stderr_tail.splitlines()) <= ffmpeg.STDERR_TAIL_LINES
    assert log.exists() and "missing.mp4" in log.read_text()


@needs_ffmpeg
def test_run_timeout(tmp_path):
    with pytest.raises(ffmpeg.FFmpegTimeout):
        ffmpeg.ffmpeg(
            ["-re", "-f", "lavfi", "-i", "testsrc2=duration=30", "-f", "null", "-"], timeout=0.5
        )


@needs_ffmpeg
def test_run_cancel():
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(ffmpeg.FFmpegCancelled):
        ffmpeg.ffmpeg(
            ["-re", "-f", "lavfi", "-i", "testsrc2=duration=30", "-f", "null", "-"], cancel=cancel
        )
    assert time.monotonic() - started < 5


def test_missing_binary(monkeypatch):
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda _b: None)
    with pytest.raises(ffmpeg.FFmpegNotFound):
        ffmpeg.run("ffmpeg", ["-version"])


def test_sanitize_argv_redacts_and_truncates():
    s = ffmpeg.sanitize_argv(["ffmpeg", "-headers", "Authorization: Bearer abc.def", "x" * 500])
    assert "abc.def" not in s
    assert "...(+" in s


def test_parse_probe_fixture():
    data = json.loads((FIXTURES / "ffprobe_phone.json").read_text())
    info = parse_probe(data)
    assert info.video.codec == "h264"
    assert info.video.fps == pytest.approx(29.97, abs=0.01)
    assert info.video.rotation == 270
    assert info.display_size == (1080, 1920)
    assert info.audio.channels == 2
    assert info.duration == pytest.approx(12.5)


def test_parse_probe_no_audio_and_cover_art():
    data = {
        "format": {"format_name": "mp4", "duration": "3.0"},
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg", "width": 10, "height": 10,
             "disposition": {"attached_pic": 1}},
            {"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360,
             "avg_frame_rate": "25/1"},
        ],
    }  # fmt: skip
    info = parse_probe(data)
    assert info.audio is None
    assert info.video.codec == "h264" and info.video.fps == 25


@needs_ffmpeg
def test_probe_real(synthetic_video):
    info = probe(synthetic_video)
    assert info.video.width == 640 and info.video.height == 360
    assert info.audio is not None
    assert info.duration == pytest.approx(6.0, abs=0.1)


def test_parse_encoders():
    text = (FIXTURES / "ffmpeg_encoders.txt").read_text()
    names = ffmpeg.parse_encoders(text)
    assert {"libx264", "h264_videotoolbox", "aac"} <= names
    assert "=" not in names


def test_select_encoder():
    assert select_encoder("auto", {"libx264", "h264_videotoolbox"}).name == "videotoolbox"
    assert select_encoder("auto", {"libx264"}).name == "x264"
    assert select_encoder("x264", {"libx264", "h264_videotoolbox"}).name == "x264"
    with pytest.raises(EncoderUnavailable):
        select_encoder("videotoolbox", {"libx264"})
    with pytest.raises(EncoderUnavailable):
        select_encoder("auto", {"aac"})
    args = select_encoder("auto", {"libx264"}).video_args(fps=30)
    assert args[:2] == ["-c:v", "libx264"] and "yuv420p" in args


def test_parse_scene_times():
    text = "frame:0 pts:0 pts_time:2.5\nlavfi.scene_score=0.5\nframe:1 pts:1 pts_time:7.04\n"
    assert ffmpeg.parse_scene_times(text) == [2.5, 7.04]


def test_requirements_matching():
    caps = WorkerCapabilities(
        os="Darwin", arch="arm64", cpu_threads=8, ram_mb=16000, has_ffmpeg=True,
        has_ffprobe=True, has_videotoolbox=True, available_encoders=["h264_videotoolbox"], working_encoders=["h264_videotoolbox"],
        available_transcribers=["fake", "mlx"], tags=["os:darwin"],
    )  # fmt: skip
    assert satisfies(caps, TaskRequirements(transcriber="mlx", encoder="any_h264")) == []
    problems = satisfies(caps, TaskRequirements(min_ram_mb=32000, encoder="libx264", tags=["gpu"]))
    assert len(problems) == 3


@needs_ffmpeg
def test_fonts_dir_with_special_chars_survives_filtergraph(tmp_path):
    """Путь с : ' [ ] , ; в fontsdir — два уровня экранирования ffmpeg."""
    from clipfactory.media.filters import escape_filter_value

    fonts = tmp_path / "шрифты: it's [x],y;z"
    fonts.mkdir()
    (tmp_path / "captions.ass").write_text(
        "[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Start, End, Style, Text\n",
        encoding="utf-8",
    )
    graph = f"[0:v]ass=filename=captions.ass:fontsdir={escape_filter_value(str(fonts))}[v]"
    ffmpeg.ffmpeg(
        ["-f", "lavfi", "-i", "color=s=64x64:d=1", "-filter_complex", graph,
         "-map", "[v]", "-frames:v", "1", "out.png"],
        cwd=tmp_path,
    )  # fmt: skip
    assert (tmp_path / "out.png").exists()


def test_select_encoder_requires_working_encoder():
    listed = {"libx264", "h264_nvenc"}
    no_gpu = lambda name: name != "h264_nvenc"  # noqa: E731
    assert select_encoder("auto", listed, works=no_gpu).name == "x264"
    assert select_encoder("auto", listed).name == "nvenc"  # GPU есть -> nvenc раньше x264
    with pytest.raises(EncoderUnavailable):
        select_encoder("nvenc", listed, works=no_gpu)
    with pytest.raises(EncoderUnavailable):
        select_encoder("hevc_magic", listed)
    args = select_encoder("nvenc", listed).video_args(fps=30)
    assert args[:2] == ["-c:v", "h264_nvenc"] and "yuv420p" in args


@needs_ffmpeg
def test_encoder_works_probe():
    assert ffmpeg.encoder_works("libx264") is True
    assert ffmpeg.encoder_works("definitely_not_an_encoder") is False
