from pathlib import Path

import pytest

from clipfactory.media import ffmpeg
from clipfactory.media.probe import probe
from clipfactory.pipeline.context import SourceError
from clipfactory.pipeline.ingest import IngestStage
from clipfactory.pipeline.render import enforce_campaign_rules
from clipfactory.schemas import Campaign, ClipCandidate, PlatformClipMeta
from tests.conftest import make_synthetic_video, needs_ffmpeg
from tests.helpers import make_app


def _ctx(tmp_path, source):
    app = make_app(tmp_path)
    job = app.create_job(source, "example")
    return app.context(job)


@needs_ffmpeg
def test_ingest_copies_mp4_without_reencode(tmp_path, synthetic_video):
    ctx = _ctx(tmp_path, str(synthetic_video))
    res = IngestStage().run(ctx)
    assert res.info["remuxed"] is False
    out = ctx.storage.local_path(ctx.key("source.mp4"))
    assert out.read_bytes() == Path(synthetic_video).read_bytes()


@needs_ffmpeg
def test_ingest_remuxes_mkv(tmp_path, synthetic_video):
    mkv = tmp_path / "видео.mkv"
    ffmpeg.ffmpeg(["-i", str(synthetic_video), "-c", "copy", str(mkv)])
    ctx = _ctx(tmp_path, str(mkv))
    res = IngestStage().run(ctx)
    assert res.info["remuxed"] is True
    info = probe(ctx.storage.local_path(ctx.key("source.mp4")))
    assert "mp4" in info.format_name and info.video.codec == "h264"


@needs_ffmpeg
def test_ingest_url_routing(tmp_path, synthetic_video):
    calls = []

    def fake_http(url, dest):
        calls.append(("http", url))
        return Path(synthetic_video)

    def fake_ytdlp(url, dest):
        calls.append(("ytdlp", url))
        return Path(synthetic_video)

    stage = IngestStage(http_downloader=fake_http, ytdlp_downloader=fake_ytdlp)
    stage.run(_ctx(tmp_path / "a", "https://cdn.example.com/v/file.mp4"))
    stage.run(_ctx(tmp_path / "b", "https://www.youtube.com/watch?v=abc"))
    assert [c[0] for c in calls] == ["http", "ytdlp"]


def test_ingest_missing_file(tmp_path):
    ctx = _ctx(tmp_path, str(tmp_path / "nope.mp4"))
    with pytest.raises(SourceError):
        IngestStage().run(ctx)


@needs_ffmpeg
def test_ingest_rejects_audio_only(tmp_path):
    wav = tmp_path / "a.wav"
    ffmpeg.ffmpeg(["-f", "lavfi", "-i", "sine=duration=1", str(wav)])
    with pytest.raises(SourceError, match="no video"):
        IngestStage().run(_ctx(tmp_path, str(wav)))


def test_campaign_rules_enforced():
    camp = Campaign(
        id="c", name="c", rate_per_1k_views=1, platforms=["youtube"],
        must_include_tags=["#shorts"], mentions=["@brand"], forbidden=["казино"],
    )  # fmt: skip
    cand = ClipCandidate(id="c01", start=0, end=30, score=50, title="Запасной заголовок")
    raw = PlatformClipMeta(
        platform="youtube",
        title="Лучшее казино года",
        description="Смотри до конца. Ставки в казино тут. Подпишись!",
        hashtags=["#Казино", "подкаст", "#shorts", "# "],
    )
    meta = enforce_campaign_rules(raw, camp, cand)
    assert meta.title == "Запасной заголовок"
    assert "казино" not in meta.description.lower()
    assert meta.description.endswith("@brand")
    assert meta.hashtags == ["#shorts", "#подкаст"]


@needs_ffmpeg
def test_render_without_audio(tmp_path):
    src = make_synthetic_video(tmp_path / "silent.mp4", duration=40, audio=False)
    app = make_app(tmp_path)
    job = app.create_job(str(src), "example")
    from clipfactory.pipeline.orchestrator import JobFailed

    # без аудио транскрибировать нечего — понятная ошибка на этапе transcribe
    with pytest.raises(JobFailed) as ei:
        app.run_job(job.id)
    assert ei.value.stage.value == "transcribe"
    failed = app.db.get_job(job.id)
    assert failed.failed_stage.value == "transcribe" and failed.retryable is False


def test_download_error_becomes_retryable_source_error(tmp_path):
    from clipfactory.backends.downloader import DownloadError

    def failing(url, dest):
        raise DownloadError("connection reset", retryable=True)

    stage = IngestStage(http_downloader=failing, ytdlp_downloader=failing)
    with pytest.raises(SourceError) as ei:
        stage.run(_ctx(tmp_path, "https://cdn.example.com/v.mp4"))
    assert ei.value.retryable is True
