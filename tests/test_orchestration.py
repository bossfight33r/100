"""Фаза 2: манифесты, кеш, resume/retry, stage_runs, RQ, восстановление."""

from __future__ import annotations

import json

import pytest

from clipfactory.backends.llm.fake import FakeLLM
from clipfactory.backends.transcriber.fake import FakeTranscriber
from clipfactory.pipeline.orchestrator import JobFailed
from clipfactory.pipeline.render import RenderStage
from clipfactory.schemas import JobStatus, StageName
from tests.conftest import make_synthetic_video, needs_ffmpeg
from tests.helpers import make_app

pytestmark = [needs_ffmpeg, pytest.mark.slow]

FAST_CAMPAIGN = """
id: fast
name: Fast test campaign
rate_per_1k_views: 2
platforms: [youtube]
clip_min_sec: 8
clip_max_sec: 12
clip_count: 1
language: ru
"""


class CountingTranscriber(FakeTranscriber):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def transcribe(self, audio_path, language=None):
        self.calls += 1
        return super().transcribe(audio_path, language)


@pytest.fixture(scope="module")
def short_video(tmp_path_factory):
    return make_synthetic_video(tmp_path_factory.mktemp("v") / "short.mp4", duration=25)


@pytest.fixture
def env(tmp_path, short_video):
    cdir = tmp_path / "campaigns"
    cdir.mkdir()
    (cdir / "fast.yaml").write_text(FAST_CAMPAIGN, encoding="utf-8")
    tr = CountingTranscriber()
    llm = FakeLLM(clip_len=10, per_chunk=2)
    app = make_app(tmp_path, transcriber=tr, llm=llm, campaigns_dir=cdir)
    job = app.create_job(str(short_video), "fast")
    return app, job.id, tr, llm


def runs_by_stage(app, job_id, since=0):
    out = {}
    for r in app.db.stage_runs(job_id)[since:]:
        out[r["stage"]] = "cache" if r["cached"] else r["status"]
    return out


def test_second_run_is_fully_cached(env):
    app, job_id, tr, llm = env
    app.run_job(job_id)
    n_runs, n_llm = len(app.db.stage_runs(job_id)), len(llm.calls)
    manifest = json.loads(app.storage.local_path(f"jobs/{job_id}/manifest.json").read_text())
    assert set(manifest) == {s.value for s in StageName}
    for m in manifest.values():
        assert m["status"] == "completed" and m["output_hashes"] and m["config_hash"]

    app.run_job(job_id)
    assert set(runs_by_stage(app, job_id, n_runs).values()) == {"cache"}
    assert tr.calls == 1 and len(llm.calls) == n_llm
    assert app.db.get_job(job_id).status == JobStatus.awaiting_review


def test_acceptance_broken_render_then_retry(env, monkeypatch):
    """Сломали render -> retry: ingest/transcribe/select не выполняются заново."""
    app, job_id, tr, llm = env
    original = RenderStage.run
    calls = {"n": 0}

    def broken(self, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated render crash")
        return original(self, ctx)

    monkeypatch.setattr(RenderStage, "run", broken)
    with pytest.raises(JobFailed):
        app.run_job(job_id)
    job = app.db.get_job(job_id)
    assert job.status == JobStatus.failed and job.failed_stage == StageName.render
    assert job.retryable is True
    n_runs, n_llm = len(app.db.stage_runs(job_id)), len(llm.calls)

    app.retry_job(job_id)  # InlineQueue — выполняется сразу
    runs = runs_by_stage(app, job_id, n_runs)
    assert runs["ingest"] == runs["transcribe"] == runs["select"] == "cache"
    assert runs["reframe"] == runs["captions"] == "cache"
    assert runs["render"] == "completed"
    assert tr.calls == 1
    # LLM после падения вызывалась только для метаданных render, не для select
    assert all("TASK: metadata" in s for s, _ in llm.calls[n_llm:])
    assert app.db.get_job(job_id).status == JobStatus.awaiting_review


def test_tampered_output_and_config_change_invalidate(env):
    app, job_id, tr, llm = env
    app.run_job(job_id)
    # 1) подменили highlights.json -> select пересчитывается, ingest/transcribe — кеш
    key = f"jobs/{job_id}/highlights.json"
    data = json.loads(app.storage.local_path(key).read_text())
    data["candidates"][0]["score"] = 1
    app.storage.put_bytes(key, json.dumps(data).encode())
    n = len(app.db.stage_runs(job_id))
    app.run_job(job_id)
    runs = runs_by_stage(app, job_id, n)
    assert runs["ingest"] == runs["transcribe"] == "cache"
    assert runs["select"] == "completed"

    # 2) удалили final.mp4 -> только render
    clip = app.db.list_clips(job_id)[0]
    app.storage.delete(clip.video_key)
    n = len(app.db.stage_runs(job_id))
    app.run_job(job_id)
    runs = runs_by_stage(app, job_id, n)
    assert runs["render"] == "completed"
    assert {runs[s] for s in ("ingest", "transcribe", "select", "reframe", "captions")} == {"cache"}

    # 3) смена шрифта -> captions и render, остальное кеш
    app.settings.caption_font = "DejaVu Sans Mono"
    n = len(app.db.stage_runs(job_id))
    app.run_job(job_id)
    runs = runs_by_stage(app, job_id, n)
    assert runs["captions"] == "completed" and runs["reframe"] == "cache"
    assert tr.calls == 1


def test_force_stage_and_no_cache(env):
    app, job_id, tr, _ = env
    app.run_job(job_id)
    n = len(app.db.stage_runs(job_id))
    app.run_job(job_id, force_stage=StageName.reframe)
    runs = runs_by_stage(app, job_id, n)
    assert runs["select"] == "cache"
    assert runs["reframe"] == runs["captions"] == runs["render"] == "completed"

    n = len(app.db.stage_runs(job_id))
    app.run_job(job_id, no_cache=True)
    assert set(runs_by_stage(app, job_id, n).values()) == {"completed"}
    assert tr.calls == 2


def test_rq_worker_recovers_after_crash(env, monkeypatch, tmp_path):
    """Воркер упал посреди render, Redis потерян: recover() ставит job заново из SQLite."""
    fakeredis = pytest.importorskip("fakeredis")
    from rq import SimpleWorker

    from clipfactory.queue.base import TaskStatus
    from clipfactory.queue.rq import RQQueue
    from clipfactory.worker import recover

    app, job_id, _, _ = env
    # симуляция: job дошёл до render и воркер умер (stage_run остался running)
    app.db.set_job_status(job_id, JobStatus.rendering)
    app.db.start_stage_run(job_id, StageName.render, 1, "x")

    conn = fakeredis.FakeStrictRedis()
    queue = RQQueue(connection=conn)
    assert queue.status("run-" + job_id) == TaskStatus.unknown

    # execute_task строит App из окружения — направляем его на тестовые каталоги и fake
    s = app.settings
    for k, v in {
        "CF_DATA_DIR": s.data_dir, "CF_CAMPAIGNS_DIR": s.campaigns_dir,
        "CF_ACCOUNTS_FILE": s.accounts_file, "CF_TRANSCRIBER": "fake",
        "CF_LLM_PROVIDER": "fake", "CF_FACE_DETECTOR": "fake", "CF_ENCODER": "x264",
        "CF_CAPTION_FONT": "DejaVu Sans", "CF_ANALYSIS_FPS": "2",
    }.items():  # fmt: skip
        monkeypatch.setenv(k, str(v))
    monkeypatch.chdir(tmp_path)

    assert recover(app, queue) == [job_id]
    assert queue.status("run-" + job_id) == TaskStatus.queued
    assert recover(app, queue) == []  # повторный recover не дублирует задачу

    SimpleWorker([queue.rq_queue], connection=conn).work(burst=True)
    assert queue.status("run-" + job_id) == TaskStatus.finished
    assert app.db.get_job(job_id).status == JobStatus.awaiting_review
    statuses = [r["status"] for r in app.db.stage_runs(job_id)]
    assert "abandoned" in statuses and "running" not in statuses


def test_rq_queue_cancel():
    fakeredis = pytest.importorskip("fakeredis")
    from clipfactory.queue.base import Task, TaskStatus
    from clipfactory.queue.rq import RQQueue

    q = RQQueue(connection=fakeredis.FakeStrictRedis())
    tid = q.enqueue(Task(kind="run_job", payload={"job_id": "x"}))
    assert q.status(tid) == TaskStatus.queued
    assert q.cancel(tid) is True
    assert q.status(tid) == TaskStatus.cancelled
    assert q.cancel("missing") is False


def test_enqueue_rejects_duplicate_task(env):
    from clipfactory.queue.base import TaskStatus
    from clipfactory.services import JobBusy

    app, job_id, _, _ = env

    class BusyQueue:
        def status(self, task_id):
            return TaskStatus.queued

        def enqueue(self, task):  # pragma: no cover - не должен вызываться
            raise AssertionError("duplicate enqueue")

    app.__dict__["queue"] = BusyQueue()
    with pytest.raises(JobBusy):
        app.enqueue_job(job_id)


def test_cli_unknown_campaign_is_clean_error(tmp_path, monkeypatch, short_video):
    from typer.testing import CliRunner

    from clipfactory.cli import app as cli

    monkeypatch.setenv("CF_DATA_DIR", str(tmp_path / "d"))
    res = CliRunner().invoke(cli, ["run", str(short_video), "--campaign", "nope"])
    assert res.exit_code == 1 and "unknown campaign" in res.output
    assert "Traceback" not in res.output
