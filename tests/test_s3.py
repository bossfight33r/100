"""S3Storage и весь pipeline на S3 (moto — локальный мок, без сети)."""

from __future__ import annotations

import pytest

pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from clipfactory.media.probe import probe  # noqa: E402
from clipfactory.schemas import JobStatus  # noqa: E402
from clipfactory.storage import ObjectNotFound, ObjectStorage, StorageError  # noqa: E402
from clipfactory.storage.s3 import S3Storage  # noqa: E402
from tests.conftest import make_synthetic_video, needs_ffmpeg  # noqa: E402
from tests.helpers import make_fast_app  # noqa: E402

BUCKET = "cf-test"


@pytest.fixture
def s3(monkeypatch):
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    with moto.mock_aws():
        import boto3

        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def test_s3_storage_contract(s3, tmp_path):
    st = S3Storage(BUCKET, prefix="pfx", client=s3)
    assert isinstance(st, ObjectStorage)
    src = tmp_path / "a.bin"
    src.write_bytes(b"hello")
    st.put_file("jobs/j/a.bin", src)
    st.put_bytes("jobs/j/b.json", b"{}")
    assert st.exists("jobs/j/a.bin") and not st.exists("jobs/j/none")
    assert s3.head_object(Bucket=BUCKET, Key="pfx/jobs/j/a.bin")["ContentLength"] == 5
    import hashlib

    assert st.checksum("jobs/j/a.bin") == hashlib.sha256(b"hello").hexdigest()
    assert st.get_file("jobs/j/a.bin", tmp_path / "out" / "x").read_bytes() == b"hello"
    assert st.open_read("jobs/j/b.json").read() == b"{}"
    assert st.stat("jobs/j/a.bin").size == 5
    # объект, загруженный не нами (без метаданных) — хеш считается потоково
    s3.put_object(Bucket=BUCKET, Key="pfx/jobs/j/foreign", Body=b"xyz")
    assert st.checksum("jobs/j/foreign") == hashlib.sha256(b"xyz").hexdigest()
    st.delete("jobs/j/a.bin")
    st.delete("jobs/j/a.bin")  # идемпотентно
    with pytest.raises(ObjectNotFound):
        st.checksum("jobs/j/a.bin")
    with pytest.raises(ObjectNotFound):
        st.get_file("jobs/j/a.bin", tmp_path / "y")
    with pytest.raises(StorageError):
        st.exists("../escape")


@needs_ffmpeg
@pytest.mark.slow
def test_full_pipeline_on_s3(s3, tmp_path):
    src = make_synthetic_video(tmp_path / "v.mp4", duration=30)
    app = make_fast_app(tmp_path, storage="s3", s3_bucket=BUCKET, s3_prefix="cf")
    assert isinstance(app.storage, S3Storage)
    job = app.create_job(str(src), "fast")
    app.run_job(job.id)
    assert app.db.get_job(job.id).status == JobStatus.awaiting_review

    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix="cf/")["Contents"]}
    clips = app.db.list_clips(job.id)
    assert clips
    for c in clips:
        for name in ("final.mp4", "thumb.jpg", "meta.json", "reframe.json", "captions.ass"):
            assert f"cf/jobs/{job.id}/clips/{c.clip_id}/{name}" in keys
        info = probe(app.materialize(c.video_key))
        assert (info.video.width, info.video.height) == (1080, 1920)
    assert not (tmp_path / "data" / "jobs").exists()  # на диск артефакты не пишутся
    assert app.location(f"jobs/{job.id}") == f"s3://{BUCKET}/cf/jobs/{job.id}"

    # повторный прогон — весь кеш берётся из S3 по метаданным sha256
    n = len(app.db.stage_runs(job.id))
    app.run_job(job.id)
    assert {r["cached"] for r in app.db.stage_runs(job.id)[n:]} == {1}

    # публикация/экспорт берут файл через materialize (кеш по sha256)
    from clipfactory.pipeline.review import ReviewService

    ReviewService(app).approve(job.id, clips[0].clip_id)
    p1 = app.materialize(clips[0].video_key)
    assert app.materialize(clips[0].video_key) == p1 and p1.stat().st_size > 0


@needs_ffmpeg
@pytest.mark.slow
def test_ffmpeg_logs_survive_scratch_cleanup_on_s3(s3, tmp_path, monkeypatch):
    from clipfactory.pipeline.orchestrator import JobFailed
    from clipfactory.pipeline.render import RenderStage

    src = make_synthetic_video(tmp_path / "v.mp4", duration=30)
    app = make_fast_app(tmp_path, storage="s3", s3_bucket=BUCKET)
    job = app.create_job(str(src), "fast")

    def bad_render(self, ctx, cand, *a, **k):
        from clipfactory.media import ffmpeg

        ffmpeg.ffmpeg(["-i", "/nonexistent.mp4", "-f", "null", "-"],
                      log_path=ctx.log_path("render", f"-{cand.id}"))  # fmt: skip

    monkeypatch.setattr(RenderStage, "_render_clip", bad_render)
    with pytest.raises(JobFailed):
        app.run_job(job.id)
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
    assert any(k.startswith(f"jobs/{job.id}/logs/render-") for k in keys)
    failed = app.db.get_job(job.id)
    assert failed.error_type == "ffmpeg_error"
    # путь к логу в ошибке ведёт в хранилище, а не в удалённую временную папку
    assert f"s3://{BUCKET}/jobs/{job.id}/logs/render-" in failed.error_message
    assert "/tmp/cf-" not in failed.error_message
