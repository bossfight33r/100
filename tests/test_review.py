import pytest

from clipfactory.pipeline.review import ReviewError, ReviewService
from clipfactory.schemas import ClipMeta, ClipStatus, JobStatus, Platform
from tests.conftest import make_synthetic_video, needs_ffmpeg
from tests.helpers import make_fast_app

pytestmark = [needs_ffmpeg, pytest.mark.slow]


@pytest.fixture
def reviewed(tmp_path):
    src = make_synthetic_video(tmp_path / "v.mp4", duration=30)
    app = make_fast_app(tmp_path)
    job = app.create_job(str(src), "fast")
    app.run_job(job.id)
    return app, job.id, ReviewService(app)


def test_approve_reject_logged(reviewed):
    app, job_id, svc = reviewed
    c1, c2 = [c.clip_id for c in app.db.list_clips(job_id)]
    svc.approve(job_id, c1, actor="tg:1")
    svc.reject(job_id, c2, actor="tg:1", reason="скучно")
    statuses = {c.clip_id: c.status for c in app.db.list_clips(job_id)}
    assert statuses == {c1: ClipStatus.approved, c2: ClipStatus.rejected}
    actions = app.db.review_actions(job_id)
    assert [a["action"] for a in actions] == ["approve", "reject"]
    assert actions[1]["actor"] == "tg:1" and "скучно" in actions[1]["payload"]


def test_edit_metadata_override_keeps_artifact(reviewed):
    app, job_id, svc = reviewed
    clip = app.db.list_clips(job_id)[0]
    before = app.storage.local_path(clip.meta_key).read_bytes()
    metas = svc.edit_metadata(
        job_id, clip.clip_id, title="Новый заголовок", hashtags=["#тест"], platform=Platform.youtube
    )
    yt = next(m for m in metas if m.platform == Platform.youtube)
    tt = next(m for m in metas if m.platform == Platform.tiktok)
    assert yt.title == "Новый заголовок"
    assert yt.hashtags == ["#shorts", "#тест"]  # обязательный тег кампании вернулся
    assert tt.title != "Новый заголовок"
    assert app.storage.local_path(clip.meta_key).read_bytes() == before  # артефакт render не тронут
    assert svc.effective_meta(job_id, clip.clip_id)[0].title == "Новый заголовок"


def test_rerender_captions_only_reruns_captions_and_render(reviewed):
    app, job_id, svc = reviewed
    clip = app.db.list_clips(job_id)[0]
    svc.approve(job_id, clip.clip_id)
    meta_before = ClipMeta.model_validate_json(app.storage.local_path(clip.meta_key).read_bytes())
    n = len(app.db.stage_runs(job_id))
    svc.rerender_captions(job_id, clip.clip_id)
    runs = {
        r["stage"]: ("cache" if r["cached"] else r["status"]) for r in app.db.stage_runs(job_id)[n:]
    }
    assert runs["select"] == runs["reframe"] == "cache"
    assert runs["captions"] == runs["render"] == "completed"
    assert app.db.get_job(job_id).status == JobStatus.awaiting_review
    assert app.db.get_clip(job_id, clip.clip_id).status == ClipStatus.pending_review
    meta_after = ClipMeta.model_validate_json(app.storage.local_path(clip.meta_key).read_bytes())
    assert meta_after.platforms == meta_before.platforms  # метаданные переиспользованы


def test_rerender_crop_override(reviewed):
    app, job_id, svc = reviewed
    clip = app.db.list_clips(job_id)[0]
    svc.rerender_crop(job_id, clip.clip_id, center_x=0.9)
    from clipfactory.schemas import ReframePlan

    plan = ReframePlan.model_validate_json(
        app.storage.local_path(f"jobs/{job_id}/clips/{clip.clip_id}/reframe.json").read_bytes()
    )
    k = plan.keyframes[0]
    assert len(plan.keyframes) == 1 and k.x + k.w == plan.source_width  # упёрлись в правый край
    with pytest.raises(ReviewError):
        svc.rerender_crop(job_id, clip.clip_id, center_x=1.5)


def test_review_requires_reviewable_job(reviewed):
    app, job_id, svc = reviewed
    app.db.set_job_status(job_id, JobStatus.rendering)
    with pytest.raises(ReviewError):
        svc.approve(job_id, "c01")
    app.db.set_job_status(job_id, JobStatus.awaiting_review)
    with pytest.raises(ReviewError):
        svc.approve(job_id, "nope")


def test_rerender_crop_rerenders_only_that_clip(reviewed, monkeypatch):
    from clipfactory.media import ffmpeg as ff

    app, job_id, svc = reviewed
    c1, c2 = app.db.list_clips(job_id)
    before = {c.clip_id: app.storage.checksum(c.video_key) for c in (c1, c2)}
    rendered = []
    real = ff.ffmpeg

    def spy(args, **kw):
        if "-filter_complex" in args:
            rendered.append(str(kw.get("cwd")))
        return real(args, **kw)

    monkeypatch.setattr(ff, "ffmpeg", spy)
    svc.rerender_crop(job_id, c1.clip_id, center_x=0.9)
    assert len(rendered) == 1 and rendered[0].endswith(c1.clip_id)  # только c01
    assert app.storage.checksum(c2.video_key) == before[c2.clip_id]
    assert app.storage.checksum(c1.video_key) != before[c1.clip_id]

    rendered.clear()
    app.run_job(job_id, no_cache=True)
    assert len(rendered) == 2  # --no-cache игнорирует и кеш клипов


def test_cli_review_actions(reviewed, monkeypatch):
    from typer.testing import CliRunner

    from clipfactory.cli import app as cli
    from clipfactory.services import App

    app, job_id, _ = reviewed
    monkeypatch.setattr("clipfactory.cli._app", lambda: App(app.settings))
    runner = CliRunner()
    clip = app.db.list_clips(job_id)[0].clip_id
    res = runner.invoke(cli, ["review", job_id, clip, "edit", "--title", "CLI заголовок",
                              "--hashtags", "#a #b", "--platform", "youtube"])  # fmt: skip
    assert res.exit_code == 0, res.output
    yt = next(
        m for m in ReviewService(app).effective_meta(job_id, clip) if m.platform.value == "youtube"
    )
    assert yt.title == "CLI заголовок" and "#a" in yt.hashtags
    assert runner.invoke(cli, ["review", job_id, clip, "edit"]).exit_code == 1
    res = runner.invoke(cli, ["review", job_id, clip, "crop", "--center", "10"])
    assert res.exit_code == 0, res.output
    assert app.load_overrides(job_id).crop_center_x[clip] == 0.1
    res = runner.invoke(cli, ["review", job_id, clip, "reject", "--reason", "плохо"])
    assert res.exit_code == 0 and app.db.get_clip(job_id, clip).status.value == "rejected"
    assert runner.invoke(cli, ["review", job_id, clip, "dance"]).exit_code == 1


def test_parse_interval():
    import typer

    from clipfactory.cli import parse_interval

    assert parse_interval("30m") == 1800 and parse_interval("6h") == 21600
    assert parse_interval("1d") == 86400 and parse_interval("600") == 600
    for bad in ("1m", "abc", "h", ""):
        with pytest.raises(typer.BadParameter):
            parse_interval(bad)


def test_rerender_on_busy_job_changes_nothing(reviewed):
    app, job_id, svc = reviewed
    clip = app.db.list_clips(job_id)[0]
    svc.approve(job_id, clip.clip_id)
    app.job_busy = lambda jid: True  # задача уже в очереди/выполняется
    with pytest.raises(ReviewError, match="already queued"):
        svc.rerender_crop(job_id, clip.clip_id, center_x=0.3)
    with pytest.raises(ReviewError):
        svc.rerender_captions(job_id, clip.clip_id)
    assert app.load_overrides(job_id).crop_center_x == {}
    assert app.db.get_clip(job_id, clip.clip_id).status == ClipStatus.approved
