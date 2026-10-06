"""Маршрутизация задач по тегам воркеров (fakeredis, без сети)."""

from __future__ import annotations

import pytest

from clipfactory.compute.capabilities import TaskRequirements, WorkerCapabilities
from clipfactory.compute.routing import (
    Heartbeat,
    derive_tags,
    eligible_workers,
    live_workers,
    publish_heartbeat,
    queue_for,
    queues_for_worker,
)
from clipfactory.queue.base import Task

fakeredis = pytest.importorskip("fakeredis")


def caps(**kw) -> WorkerCapabilities:
    base = dict(os="Linux", arch="x86_64", cpu_threads=8, ram_mb=16000, has_ffmpeg=True,
                has_ffprobe=True, has_videotoolbox=False, available_encoders=[],
                working_encoders=["libx264"], available_transcribers=["fake"], tags=[])  # fmt: skip
    base.update(kw)
    return WorkerCapabilities(**base)


def test_tags_from_real_capabilities():
    assert derive_tags(caps()) == []
    gpu = caps(working_encoders=["h264_nvenc", "libx264"])
    assert derive_tags(gpu, ["eu"]) == ["eu", "gpu"]
    mac = caps(working_encoders=["h264_videotoolbox"], available_transcribers=["fake", "mlx"])
    assert derive_tags(mac) == ["mac", "mlx"]
    # nvenc в списке ffmpeg, но не кодирует — тега gpu нет
    assert derive_tags(caps(available_encoders=["h264_nvenc"])) == []


def test_queue_names():
    assert queue_for(TaskRequirements()) == "clipfactory"
    assert queue_for(TaskRequirements(tags=["mlx", "gpu", "gpu"])) == "clipfactory@gpu+mlx"
    names = queues_for_worker(["mlx", "gpu"])
    assert names == ["clipfactory@gpu+mlx", "clipfactory@gpu", "clipfactory@mlx", "clipfactory"]
    # воркер слушает очередь любой задачи, чьи теги — подмножество его тегов
    for req_tags in ([], ["gpu"], ["mlx"], ["gpu", "mlx"]):
        assert queue_for(TaskRequirements(tags=req_tags)) in names
    assert queue_for(TaskRequirements(tags=["mac"])) not in names


def test_heartbeat_registry_and_eligibility():
    conn = fakeredis.FakeStrictRedis()
    publish_heartbeat(conn, "linux-1", caps(tags=["gpu"], working_encoders=["h264_nvenc"]))
    publish_heartbeat(conn, "mac-1", caps(tags=["mac"]))
    workers = live_workers(conn)
    assert set(workers) == {"linux-1", "mac-1"}
    assert eligible_workers(TaskRequirements(tags=["gpu"]), workers) == ["linux-1"]
    assert eligible_workers(TaskRequirements(), workers) == ["linux-1", "mac-1"]
    assert conn.ttl("cf:worker:mac-1") > 0  # мёртвый воркер исчезнет сам
    with Heartbeat(conn, "tmp-1", caps()):
        assert "tmp-1" in live_workers(conn)
    assert "tmp-1" not in live_workers(conn)


def test_gpu_task_only_taken_by_gpu_worker():
    from rq import SimpleWorker

    from clipfactory.queue.rq import RQQueue

    conn = fakeredis.FakeStrictRedis()
    q = RQQueue(connection=conn)
    tid = q.enqueue(Task(kind="noop", payload={}, requirements=TaskRequirements(tags=["gpu"])))
    assert q.queue("clipfactory@gpu").count == 1 and q.rq_queue.count == 0

    cpu_worker = SimpleWorker([q.queue(n) for n in queues_for_worker([])], connection=conn)
    cpu_worker.work(burst=True)
    assert q.queue("clipfactory@gpu").count == 1  # CPU-воркер не тронул

    names = queues_for_worker(["gpu"])
    assert q.queue(names[0]).job_ids == [tid]


def test_enqueue_job_routes_by_settings(tmp_path, monkeypatch):
    from clipfactory.queue.rq import RQQueue
    from tests.helpers import make_fast_app

    app = make_fast_app(tmp_path, job_require_tags=["gpu"])
    q = RQQueue(connection=fakeredis.FakeStrictRedis())
    app.__dict__["queue"] = q
    job = app.create_job(str(tmp_path / "v.mp4"), "fast")
    app.enqueue_job(job.id)
    assert q.queue("clipfactory@gpu").job_ids == [f"run-{job.id}"]
    assert app.job_busy(job.id)  # статус ищется по id, независимо от очереди


def test_settings_parse_tag_lists(monkeypatch, tmp_path):
    from clipfactory.config import Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CF_WORKER_TAGS", "eu, gpu")
    monkeypatch.setenv("CF_JOB_REQUIRE_TAGS", "gpu")
    s = Settings()
    assert s.worker_tags == ["eu", "gpu"] and s.job_require_tags == ["gpu"]


def test_recover_keeps_original_job_requirements(tmp_path):
    """Job поставлен с gpu; восстанавливает CPU-воркер без тегов — job остаётся в gpu-очереди."""
    from clipfactory.queue.rq import RQQueue
    from clipfactory.schemas import JobStatus
    from clipfactory.worker import recover
    from tests.helpers import make_fast_app

    client = make_fast_app(tmp_path / "client", job_require_tags=["gpu"])
    job = client.create_job(str(tmp_path / "v.mp4"), "fast")
    assert client.db.get_job(job.id).require_tags == ["gpu"]

    # «CPU-воркер» с той же БД, но без CF_JOB_REQUIRE_TAGS; job упал посреди render
    worker_app = make_fast_app(tmp_path / "client")
    worker_app.db.set_job_status(job.id, JobStatus.rendering)
    q = RQQueue(connection=fakeredis.FakeStrictRedis())
    assert recover(worker_app, q) == [job.id]
    assert q.queue("clipfactory@gpu").job_ids == [f"run-{job.id}"]
    assert q.rq_queue.count == 0
