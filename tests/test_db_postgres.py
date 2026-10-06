"""Контракт Database на SQLite и настоящем PostgreSQL 16 (локальный кластер, только unix-сокет)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipfactory.db import Database, NotFound
from clipfactory.schemas import (
    ClipRecord,
    ClipStatus,
    Job,
    JobStatus,
    PlatformClipMeta,
    Publication,
    PublicationStatus,
    ReviewAction,
    StageName,
    StatsSnapshot,
)

PG_BIN = Path("/usr/lib/postgresql/16/bin")


def _as_postgres(*cmd: str) -> None:
    subprocess.run(["runuser", "-u", "postgres", "--", *cmd], check=True, capture_output=True)


@pytest.fixture(scope="session")
def pg_server():
    if not (PG_BIN / "initdb").exists() or shutil.which("runuser") is None:
        pytest.skip("PostgreSQL 16 binaries not available")
    pytest.importorskip("psycopg")
    root = Path(tempfile.mkdtemp(prefix="cf-pg-"))
    os.chmod(root, 0o777)
    data, sock = root / "data", root / "sock"
    sock.mkdir()
    os.chmod(sock, 0o777)
    try:
        _as_postgres(
            str(PG_BIN / "initdb"), "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8"
        )
        _as_postgres(str(PG_BIN / "pg_ctl"), "-D", str(data), "-w", "-l", str(root / "pg.log"),
                     "-o", f"-k {sock} -c listen_addresses='' -p 54329", "start")  # fmt: skip
    except (subprocess.CalledProcessError, PermissionError) as e:
        pytest.skip(f"cannot start PostgreSQL: {e}")
    yield f"postgresql://postgres@/postgres?host={sock}&port=54329"
    subprocess.run(["runuser", "-u", "postgres", "--", str(PG_BIN / "pg_ctl"), "-D", str(data),
                    "-m", "immediate", "stop"], capture_output=True)  # fmt: skip
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def pg_url(pg_server):
    import psycopg

    name = f"cf_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_server, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    return pg_server.replace("/postgres?", f"/{name}?")


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, tmp_path):
    if request.param == "sqlite":
        return Database(tmp_path / "db.sqlite3")
    return Database(request.getfixturevalue("pg_url"))


def test_jobs_stage_runs_and_cancel(db):
    db.create_job(Job(id="j1", campaign_id="c", source="s"))
    assert db.get_job("j1").status == JobStatus.queued
    db.set_job_failed("j1", StageName.render, "ffmpeg_error", "boom", False)
    job = db.get_job("j1")
    assert job.failed_stage == StageName.render and job.retryable is False
    rid = db.start_stage_run("j1", StageName.render, 1, "h")
    rid2 = db.start_stage_run("j1", StageName.render, 1, "h")
    assert rid2 > rid
    db.finish_stage_run(rid, "completed", cached=True, duration_ms=5)
    db.abandon_stage_runs("j1")
    statuses = [r["status"] for r in db.stage_runs("j1")]
    assert statuses == ["completed", "abandoned"]
    assert db.list_jobs([JobStatus.failed])[0].id == "j1"
    db.request_cancel("j1")
    assert db.cancel_requested("j1")
    db.clear_cancel("j1")
    assert not db.cancel_requested("j1")
    with pytest.raises(NotFound):
        db.get_job("nope")


def test_clips_review_publications_stats(db):
    db.create_job(Job(id="j1", campaign_id="c", source="s"))
    for cid in ("c01", "c02"):
        db.upsert_clip(ClipRecord(job_id="j1", clip_id=cid, start=1.5, end=9.5, score=80, hook="х"))
    db.set_clip_status("j1", "c01", ClipStatus.approved)
    db.set_clip_meta_override(
        "j1", "c01", [PlatformClipMeta(platform="youtube", title="Т", description="о")]
    )
    c01 = db.get_clip("j1", "c01")
    assert (
        c01.status == ClipStatus.approved and c01.meta_override[0].title == "Т" and c01.end == 9.5
    )
    db.delete_clips_not_in("j1", ["c01"])
    assert [c.clip_id for c in db.list_clips("j1")] == ["c01"]
    db.add_review_action(
        ReviewAction(job_id="j1", clip_id="c01", action="approve", payload={"a": 1})
    )
    assert db.review_actions("j1")[0]["action"] == "approve"

    now = datetime.now(UTC)
    pub = Publication(id="p1", job_id="j1", clip_id="c01", campaign_id="c", platform="youtube",
                      account_id="a", scheduled_at=now, status=PublicationStatus.publishing)  # fmt: skip
    db.save_publication(pub)
    assert db.stale_publishing("j1", now + timedelta(minutes=1))[0].id == "p1"
    assert db.stale_publishing("j1", now - timedelta(hours=1)) == []
    pub.status, pub.external_id = PublicationStatus.published, "vid"
    db.save_publication(pub)
    assert db.get_publication("p1").external_id == "vid"
    assert db.list_publications(campaign_id="c", statuses=[PublicationStatus.published])

    db.add_stats(StatsSnapshot(publication_id="p1", views=10, collected_at=now))
    db.add_stats(
        StatsSnapshot(publication_id="p1", views=25, collected_at=now + timedelta(hours=1))
    )
    assert [s.views for s in db.stats_history("p1")] == [10, 25]
    assert db.latest_stats()["p1"].views == 25


def test_stats_append_only_enforced(db):
    db.create_job(Job(id="j1", campaign_id="c", source="s"))
    db.save_publication(Publication(id="p1", job_id="j1", clip_id="c01", campaign_id="c",
                                    platform="tiktok", account_id="a"))  # fmt: skip
    db.add_stats(StatsSnapshot(publication_id="p1", views=1))
    for sql in ("UPDATE stats_snapshots SET views = 2", "DELETE FROM stats_snapshots"):
        with pytest.raises(Exception, match="append-only"), db.connect() as c:
            c.execute(sql)
    assert db.latest_stats()["p1"].views == 1


def test_init_is_idempotent(db):
    target = db.url if db.postgres else db.path
    Database(target)
    Database(target)
    with db.connect() as c:
        row = c.execute("SELECT version FROM schema_version").fetchone()
    assert row["version"] == 2


def test_full_pipeline_on_postgres(pg_url, tmp_path):
    from tests.conftest import make_synthetic_video
    from tests.helpers import make_fast_app

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    src = make_synthetic_video(tmp_path / "v.mp4", duration=30)
    app = make_fast_app(tmp_path, db_url=pg_url)
    assert app.db.postgres
    job = app.create_job(str(src), "fast")
    app.run_job(job.id)
    assert app.db.get_job(job.id).status == JobStatus.awaiting_review
    assert len(app.db.list_clips(job.id)) >= 1
    n = len(app.db.stage_runs(job.id))
    app.run_job(job.id)
    assert {r["cached"] for r in app.db.stage_runs(job.id)[n:]} == {1}
