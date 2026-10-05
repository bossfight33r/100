"""Минимальный e2e: синтетика -> fake транскрипт -> fake выбор -> reframe -> captions -> render."""

import json

import pytest

from clipfactory.media.probe import probe
from clipfactory.schemas import ClipMeta, Highlights, JobStatus, ReframePlan
from tests.conftest import needs_ffmpeg
from tests.helpers import make_app

pytestmark = [needs_ffmpeg, pytest.mark.slow]


@pytest.fixture(scope="module")
def finished_job(tmp_path_factory, long_synthetic_video):
    tmp = tmp_path_factory.mktemp("e2e")
    app = make_app(tmp)
    job = app.create_job(str(long_synthetic_video), "example")
    app.run_job(job.id)
    return app, job.id


def test_job_awaits_review(finished_job):
    app, job_id = finished_job
    assert app.db.get_job(job_id).status == JobStatus.awaiting_review


def test_clip_artifacts_valid(finished_job):
    app, job_id = finished_job
    storage = app.storage
    campaign = app.settings.campaign("example")
    highlights = Highlights.model_validate_json(
        storage.local_path(f"jobs/{job_id}/highlights.json").read_bytes()
    )
    assert 1 <= len(highlights.candidates) <= campaign.clip_count
    clips = app.db.list_clips(job_id)
    assert [c.clip_id for c in clips] == [c.id for c in highlights.candidates]
    for cand in highlights.candidates:
        base = f"jobs/{job_id}/clips/{cand.id}"
        for name in ("meta.json", "captions.ass", "reframe.json", "final.mp4", "thumb.jpg"):
            assert storage.exists(f"{base}/{name}"), name
        assert campaign.clip_min_sec <= cand.duration <= campaign.clip_max_sec

        info = probe(storage.local_path(f"{base}/final.mp4"))
        assert (info.video.width, info.video.height) == (1080, 1920)
        assert info.video.codec == "h264"
        assert info.video.fps == pytest.approx(30, abs=0.1)
        assert info.audio is not None and info.audio.codec == "aac"
        assert info.duration == pytest.approx(cand.duration, abs=0.2)

        plan = ReframePlan.model_validate_json(
            storage.local_path(f"{base}/reframe.json").read_bytes()
        )
        assert plan.keyframes[0].x < 640 // 2  # fake-лицо слева -> кроп сдвинут влево

        meta = ClipMeta.model_validate_json(storage.local_path(f"{base}/meta.json").read_bytes())
        platforms = {m.platform.value for m in meta.platforms}
        assert platforms == {"youtube", "tiktok"}
        for m in meta.platforms:
            assert "#shorts" in m.hashtags
            assert "@example_brand" in m.description

        ass = storage.local_path(f"{base}/captions.ass").read_text(encoding="utf-8")
        assert "Dialogue:" in ass and "DejaVu Sans" in ass


def test_cli_status_json(finished_job, monkeypatch):
    from typer.testing import CliRunner

    from clipfactory.cli import app as cli

    app, job_id = finished_job
    monkeypatch.setenv("CF_DATA_DIR", str(app.settings.data_dir))
    monkeypatch.setenv("CF_CAMPAIGNS_DIR", str(app.settings.campaigns_dir))
    res = CliRunner().invoke(cli, ["--json", "status", job_id])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["job"]["status"] == "awaiting_review"
    assert len(data["clips"]) >= 1
