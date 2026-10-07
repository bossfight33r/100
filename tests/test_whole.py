"""selection: whole — исходник целиком одним клипом (клипы Twitch/YouTube)."""

from __future__ import annotations

import pytest

from clipfactory.pipeline.select import SelectStage
from clipfactory.schemas import Campaign, Highlights, JobStatus
from tests.conftest import needs_ffmpeg
from tests.helpers import make_app

WHOLE = """
id: w
name: W
rate_per_1k_views: 1
platforms: [youtube]
clip_min_sec: 20
clip_max_sec: {max}
clip_count: 1
selection: whole
transcribe: false
layout: fit_blur
"""


def _app(tmp_path, clip_max):
    cdir = tmp_path / "campaigns"
    cdir.mkdir()
    (cdir / "w.yaml").write_text(WHOLE.format(max=clip_max), encoding="utf-8")
    return make_app(tmp_path, campaigns_dir=cdir)


def test_whole_allows_no_transcribe():
    c = Campaign(id="c", name="c", rate_per_1k_views=1, platforms=["youtube"],
                 selection="whole", transcribe=False)  # fmt: skip
    assert c.selection.value == "whole"


@needs_ffmpeg
@pytest.mark.slow
def test_whole_short_clip_kept_entirely(tmp_path, synthetic_video):
    app = _app(tmp_path, 60)
    job = app.run_job(app.create_job(str(synthetic_video), "w").id)
    assert job.status is JobStatus.awaiting_review, job.error_message
    ctx = app.context(job)
    (cand,) = ctx.read_model(ctx.key("highlights.json"), Highlights).candidates
    assert cand.start == 0 and cand.end == pytest.approx(6.0, abs=0.1)  # короче clip_min — можно
    assert SelectStage().config(ctx)["selection"] == "whole"


@needs_ffmpeg
def test_whole_long_source_trimmed(tmp_path, long_synthetic_video):
    from clipfactory.pipeline.ingest import IngestStage
    from clipfactory.pipeline.transcribe import TranscribeStage

    app = _app(tmp_path, 30)
    ctx = app.context(app.create_job(str(long_synthetic_video), "w"))
    for stage in (IngestStage(), TranscribeStage(), SelectStage()):
        res = stage.run(ctx)
        stage.validate(ctx, res.outputs)
    (cand,) = ctx.read_model(ctx.key("highlights.json"), Highlights).candidates
    assert cand.end == 30
