"""Громкость готовых клипов: двухпроходный loudnorm попадает в -14 LUFS."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from clipfactory.media import ffmpeg
from clipfactory.media.filters import (
    loudnorm_apply_filter,
    parse_loudnorm_json,
    render_filtergraph,
)
from clipfactory.schemas import CropKeyframe
from tests.conftest import needs_ffmpeg
from tests.helpers import make_fast_app

JSON_OUT = """[Parsed_loudnorm_0 @ 0x1] \n{
\t"input_i" : "-31.22",
\t"input_tp" : "-20.10",
\t"input_lra" : "0.00",
\t"input_thresh" : "-41.22",
\t"output_i" : "-14.50",
\t"target_offset" : "0.50"
}"""


def test_parse_and_apply():
    m = parse_loudnorm_json("шум до\n" + JSON_OUT)
    assert m == {"input_i": -31.22, "input_tp": -20.1, "input_lra": 0.0,
                 "input_thresh": -41.22, "target_offset": 0.5}  # fmt: skip
    f = loudnorm_apply_filter(m)
    assert "measured_I=-31.22" in f and "linear=true" in f and "TP=-2.0" in f
    assert parse_loudnorm_json("нет json") is None
    assert parse_loudnorm_json(JSON_OUT.replace('"-31.22"', '"-inf"')) is None  # тишина


def test_graph_uses_measured_filter_or_falls_back():
    kf = [CropKeyframe(t=0, x=0, y=0, w=202, h=360)]
    base = dict(keyframes=kf, target_width=1080, target_height=1920, fps=30,
                ass_file=None, fonts_dir=None, has_audio=True)  # fmt: skip
    assert "loudnorm=I=-14.0:TP=-2.0:LRA=11.0," in render_filtergraph(**base)
    custom = "loudnorm=I=-14.0:measured_I=-30"
    assert f"[0:a]{custom},aresample" in render_filtergraph(**base, loudnorm=custom)


def _integrated_lufs_and_peak(path: Path, log: Path) -> tuple[float, float]:
    ffmpeg.ffmpeg(
        ["-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"], log_path=log
    )
    text = log.read_text(errors="replace")
    summary = text[text.rfind("Summary:") :]
    lufs = float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1))
    peak = float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", summary).group(1))
    return lufs, peak


@needs_ffmpeg
@pytest.mark.slow
@pytest.mark.parametrize("level_db", [-38, -8])  # очень тихий и слишком громкий источник
def test_rendered_clip_hits_target_loudness(tmp_path, level_db):
    src = tmp_path / "v.mp4"
    ffmpeg.ffmpeg(
        ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=30",
         "-f", "lavfi", "-i", f"sine=frequency=300:sample_rate=48000:duration=30,volume={level_db}dB",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(src)],
        timeout=120,
    )  # fmt: skip
    app = make_fast_app(tmp_path)
    job = app.create_job(str(src), "fast")
    app.run_job(job.id)
    for clip in app.db.list_clips(job.id):
        lufs, peak = _integrated_lufs_and_peak(
            app.materialize(clip.video_key), tmp_path / f"m-{clip.clip_id}.log"
        )
        assert abs(lufs - (-14.0)) <= 1.0, (clip.clip_id, lufs)
        assert peak <= -1.0, (clip.clip_id, peak)
