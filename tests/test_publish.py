import json
import stat
from datetime import UTC, datetime, timedelta

import pytest

from clipfactory.pipeline.publish import PublishService, PublishServiceError
from clipfactory.pipeline.review import ReviewService
from clipfactory.publish.base import PublishError
from clipfactory.publish.export import ExportPublisher
from clipfactory.publish.youtube import YouTubePublisher, save_token, token_path, video_body
from clipfactory.schemas import JobStatus, Platform, PublicationStatus
from tests.conftest import make_synthetic_video, needs_ffmpeg
from tests.helpers import make_fast_app

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)


class FakeHttpError(Exception):
    def __init__(self, status, reason=""):
        super().__init__(f"http {status}")
        self.resp = type("R", (), {"status": status})()
        self.content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()


class FakeInsert:
    def __init__(self, script):
        self.script = list(script)

    def next_chunk(self):
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class FakeYouTube:
    """Фейковый googleapiclient-сервис: videos().insert(...).next_chunk()."""

    def __init__(self, script=None):
        self.script = script
        self.inserts = []
        self.counter = 0

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.counter += 1
        self.inserts.append(body)
        script = self.script or [(None, None), (None, {"id": f"vid{self.counter}"})]
        return FakeInsert(script)


@pytest.fixture(scope="module")
def published_env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("pub")
    src = make_synthetic_video(tmp / "v.mp4", duration=30)
    app = make_fast_app(tmp)
    job = app.create_job(str(src), "fast")
    app.run_job(job.id)
    return app, job.id


def service(app, yt=None):
    yt = yt or FakeYouTube()
    pubs = {
        Platform.youtube: YouTubePublisher(lambda acc: yt, media_factory=lambda p: p,
                                           sleep=lambda s: None, clock=lambda: NOW),
        Platform.tiktok: ExportPublisher(Platform.tiktok, app.settings.data_dir / "exports"),
    }  # fmt: skip
    return PublishService(app, publishers=pubs, clock=lambda: NOW), yt


@needs_ffmpeg
@pytest.mark.slow
def test_only_approved_clips_are_scheduled_and_published(published_env):
    app, job_id = published_env
    svc, yt = service(app)
    with pytest.raises(PublishServiceError, match="no approved"):
        svc.schedule_job(job_id)

    review = ReviewService(app)
    c1, c2 = [c.clip_id for c in app.db.list_clips(job_id)]
    review.approve(job_id, c1)
    review.reject(job_id, c2)
    pubs = svc.schedule_job(job_id)
    assert {(p.clip_id, p.account_id) for p in pubs} == {(c1, "yt_main"), (c1, "tt_main")}
    assert app.db.get_job(job_id).status == JobStatus.scheduled
    assert svc.schedule_job(job_id) == []  # идемпотентно

    # одобренный клип отклонили после планирования — загрузка всё равно запрещена
    review.reject(job_id, c1)
    svc.publish_job(job_id)
    assert yt.inserts == []
    assert {p.status for p in app.db.list_publications(job_id=job_id)} == {PublicationStatus.failed}


@needs_ffmpeg
@pytest.mark.slow
def test_youtube_scheduled_upload_and_export_package(tmp_path):
    src = make_synthetic_video(tmp_path / "v.mp4", duration=30)
    app = make_fast_app(tmp_path)
    job = app.create_job(str(src), "fast")
    app.run_job(job.id)
    clip = app.db.list_clips(job.id)[0]
    ReviewService(app).approve(job.id, clip.clip_id)
    ReviewService(app).edit_metadata(job.id, clip.clip_id, title="Мой заголовок",
                                     platform=Platform.youtube)  # fmt: skip

    transient = [FakeHttpError(503), (None, None), (None, {"id": "abc123"})]
    svc, yt = service(app, FakeYouTube(script=transient))
    svc.schedule_job(job.id)
    done = svc.publish_job(job.id)
    assert len(done) == 2

    [body] = yt.inserts
    assert body["snippet"]["title"] == "Мой заголовок"
    assert body["status"]["privacyStatus"] == "private" and body["status"]["publishAt"].endswith(
        "Z"
    )
    assert "shorts" in body["snippet"]["tags"]

    pubs = {p.platform: p for p in app.db.list_publications(job_id=job.id)}
    yt_pub = pubs[Platform.youtube]
    assert yt_pub.external_id == "abc123" and yt_pub.status == PublicationStatus.scheduled
    tt_pub = pubs[Platform.tiktok]
    assert tt_pub.status == PublicationStatus.exported
    from pathlib import Path

    pkg = Path(tt_pub.url)
    assert (pkg / "video.mp4").exists() and (pkg / "thumb.jpg").exists()
    caption = (pkg / "caption.txt").read_text(encoding="utf-8")
    assert "#shorts" in caption
    assert app.db.get_job(job.id).status == JobStatus.scheduled

    # после publishAt YouTube публикует сам — отмечаем
    svc.clock = lambda: yt_pub.scheduled_at + timedelta(minutes=1)
    assert svc.mark_due_published() == 1
    assert app.db.get_job(job.id).status == JobStatus.published


@needs_ffmpeg
@pytest.mark.slow
def test_quota_error_keeps_publication_scheduled(tmp_path):
    src = make_synthetic_video(tmp_path / "v.mp4", duration=30)
    app = make_fast_app(tmp_path)
    job = app.create_job(str(src), "fast")
    app.run_job(job.id)
    clip = app.db.list_clips(job.id)[0]
    ReviewService(app).approve(job.id, clip.clip_id)
    svc, _ = service(app, FakeYouTube(script=[FakeHttpError(403, "quotaExceeded")]))
    svc.schedule_job(job.id)
    svc.publish_job(job.id)
    yt_pub = next(
        p for p in app.db.list_publications(job_id=job.id) if p.platform == Platform.youtube
    )
    assert yt_pub.status == PublicationStatus.scheduled and "quota" in yt_pub.error


def test_youtube_auth_error_not_retryable():
    from clipfactory.publish.base import PublishRequest
    from clipfactory.schemas import Account, PlatformClipMeta

    pub = YouTubePublisher(lambda a: FakeYouTube(script=[FakeHttpError(401)]),
                           media_factory=lambda p: p, sleep=lambda s: None, clock=lambda: NOW)  # fmt: skip
    req = PublishRequest(
        publication_id="p", account=Account(id="a", platform="youtube", name="a", token_ref="a"),
        video_path=None, thumb_path=None, scheduled_at=None,
        meta=PlatformClipMeta(platform="youtube", title="t", description="d", hashtags=["#x"]),
    )  # fmt: skip
    with pytest.raises(PublishError) as ei:
        pub.publish(req)
    assert ei.value.retryable is False
    body = video_body(req, NOW)
    assert body["status"]["privacyStatus"] == "public"


def test_token_file_permissions(tmp_path):
    from clipfactory.schemas import Account

    acc = Account(id="a", platform="youtube", name="a", token_ref="yt_main")
    path = token_path(tmp_path / "secrets", acc)
    save_token(path, '{"token": "x"}')
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with pytest.raises(PublishError):
        token_path(tmp_path, Account(id="b", platform="youtube", name="b"))


@needs_ffmpeg
@pytest.mark.slow
def test_interrupted_upload_not_retried_automatically(tmp_path):
    src = make_synthetic_video(tmp_path / "v.mp4", duration=30)
    app = make_fast_app(tmp_path)
    job = app.create_job(str(src), "fast")
    app.run_job(job.id)
    clip = app.db.list_clips(job.id)[0]
    ReviewService(app).approve(job.id, clip.clip_id)
    svc, yt = service(app)
    [yt_pub, _] = sorted(svc.schedule_job(job.id), key=lambda p: p.platform.value != "youtube")
    # процесс упал посреди загрузки
    yt_pub.status = PublicationStatus.publishing
    app.db.save_publication(yt_pub)

    svc.publish_job(job.id)
    stuck = app.db.get_publication(yt_pub.id)
    assert stuck.status == PublicationStatus.failed and "interrupted" in stuck.error
    assert yt.inserts == []
    svc.schedule_job(job.id)  # повторное планирование не воскрешает failed
    assert app.db.get_publication(yt_pub.id).status == PublicationStatus.failed

    assert [p.id for p in svc.retry_failed(job.id)] == [yt_pub.id]
    svc.publish_job(job.id)
    assert len(yt.inserts) == 1
    assert app.db.get_publication(yt_pub.id).external_id == "vid1"


@needs_ffmpeg
@pytest.mark.slow
def test_past_slot_is_replanned_not_published_immediately(tmp_path):
    src = make_synthetic_video(tmp_path / "v.mp4", duration=30)
    app = make_fast_app(tmp_path)
    job = app.create_job(str(src), "fast")
    app.run_job(job.id)
    clip = app.db.list_clips(job.id)[0]
    ReviewService(app).approve(job.id, clip.clip_id)
    svc, yt = service(app)
    pubs = svc.schedule_job(job.id)
    yt_pub = next(p for p in pubs if p.platform == Platform.youtube)
    # квота/сеть: прошло два дня, слот уже в прошлом
    later = yt_pub.scheduled_at + timedelta(days=2)
    svc.clock = lambda: later
    svc.publishers[Platform.youtube].clock = lambda: later
    svc.publish_job(job.id)
    [body] = yt.inserts
    assert body["status"]["privacyStatus"] == "private"
    assert app.db.get_publication(yt_pub.id).scheduled_at > later


@needs_ffmpeg
@pytest.mark.slow
def test_clip_status_reset_when_select_picks_new_moment(tmp_path):
    from clipfactory.schemas import ClipStatus, StageName

    src = make_synthetic_video(tmp_path / "v.mp4", duration=30)
    app = make_fast_app(tmp_path)
    job = app.create_job(str(src), "fast")
    app.run_job(job.id)
    clip = app.db.list_clips(job.id)[0]
    review = ReviewService(app)
    review.approve(job.id, clip.clip_id)
    review.edit_metadata(job.id, clip.clip_id, title="Одобренный заголовок")
    # другая LLM-выдача: c01 теперь другой момент
    from clipfactory.backends.llm.fake import FakeLLM

    other = json.dumps({"highlights": [{"start_time": 15, "end_time": 25, "score": 99,
                                        "hook_sentence": "новый"}]})  # fmt: skip
    app.llm_factory = lambda: FakeLLM(
        responder=lambda sys_, p: other if "TASK: highlights" in sys_ else "{}"
    )
    app.settings.campaign("fast").notes = "другие заметки"  # меняет config select
    app.run_job(job.id, force_stage=StageName.select)
    new = app.db.get_clip(job.id, clip.clip_id)
    assert (new.start, new.end) != (clip.start, clip.end)
    assert new.status == ClipStatus.pending_review and new.meta_override is None
