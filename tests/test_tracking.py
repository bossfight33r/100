import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from clipfactory.bot.handlers import BotController
from clipfactory.db import NotFound
from clipfactory.schemas import (
    ClipRecord,
    ClipStatus,
    Job,
    JobStatus,
    Publication,
    PublicationStatus,
    StatsSnapshot,
)
from clipfactory.track.collector import TrackError, add_manual, collect_youtube
from clipfactory.track.earnings import FlatRatePerK, strategy_for
from clipfactory.track.report import (
    build_report,
    hook_features,
    render_text,
    write_prompt_recommendations,
)
from tests.helpers import ROOT, make_fast_app

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
HOOKS = {
    "c01": "Никто не говорит об этом секрете",
    "c02": "Почему 90% новичков ошибаются?",
    "c03": "Ну вот так вот бывает в жизни иногда друзья мои",
}


class FakeYTStats:
    def __init__(self, data, fail=False):
        self.data, self.fail, self.calls = data, fail, []

    def videos(self):
        return self

    def list(self, part, id, maxResults):
        self.calls.append(id)
        self._ids = id.split(",")
        return self

    def execute(self):
        if self.fail:
            raise RuntimeError("network down")
        return {
            "items": [
                {"id": i, "statistics": {k: str(v) for k, v in self.data[i].items()}}
                for i in self._ids
                if i in self.data
            ]
        }


@pytest.fixture
def app(tmp_path):
    app = make_fast_app(tmp_path, accounts_file=ROOT / "config" / "accounts.example.yaml")
    app.db.create_job(Job(id="j1", campaign_id="fast", source="x", status=JobStatus.scheduled))
    for i, (cid, hook) in enumerate(HOOKS.items()):
        app.db.upsert_clip(
            ClipRecord(job_id="j1", clip_id=cid, status=ClipStatus.approved,
                       start=i * 10, end=i * 10 + 9, score=90 - i * 20, hook=hook)
        )  # fmt: skip
        app.db.save_publication(
            Publication(
                id=f"j1-{cid}-yt_main", job_id="j1", clip_id=cid, campaign_id="fast",
                platform="youtube", account_id="yt_main", external_id=f"vid{i}",
                scheduled_at=NOW - timedelta(days=1), status=PublicationStatus.scheduled,
            )
        )  # fmt: skip
    app.db.save_publication(
        Publication(
            id="j1-c01-tt_main", job_id="j1", clip_id="c01", campaign_id="fast",
            platform="tiktok", account_id="tt_main", scheduled_at=NOW - timedelta(days=1),
            status=PublicationStatus.exported,
        )
    )  # fmt: skip
    return app


def test_flat_rate_earnings():
    camp = type("C", (), {"rate_per_1k_views": 1.5})()
    pub = type("P", (), {"id": "p"})()
    e = FlatRatePerK().compute(camp, pub, StatsSnapshot(publication_id="p", views=12_345))
    assert e.amount == Decimal("18.52") and e.views == 12_345
    assert FlatRatePerK().compute(camp, pub, None).amount == Decimal("0.00")


def test_collect_youtube_marks_due_and_appends(app):
    yt = FakeYTStats({"vid0": {"viewCount": 10000, "likeCount": 500, "commentCount": 20},
                      "vid1": {"viewCount": 2000, "likeCount": 30, "commentCount": 1},
                      "vid2": {"viewCount": 100, "likeCount": 1, "commentCount": 0}})  # fmt: skip
    snaps = collect_youtube(app, service_factory=lambda acc: yt, clock=lambda: NOW)
    assert len(snaps) == 3 and len(yt.calls) == 1  # один батч
    assert app.db.get_publication("j1-c01-yt_main").status == PublicationStatus.published
    yt.data["vid0"]["viewCount"] = 15000
    collect_youtube(app, service_factory=lambda acc: yt, clock=lambda: NOW + timedelta(hours=6))
    history = app.db.stats_history("j1-c01-yt_main")
    assert [h.views for h in history] == [10000, 15000]  # история сохранена


def test_collector_survives_api_failure(app):
    snaps = collect_youtube(
        app, service_factory=lambda acc: FakeYTStats({}, fail=True), clock=lambda: NOW
    )
    assert snaps == []


def test_stats_are_append_only(app):
    add_manual(app, "j1-c01-tt_main", views=5000, likes=10, clock=lambda: NOW)
    with app.db.connect() as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE stats_snapshots SET views = 1")
    with app.db.connect() as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM stats_snapshots")
    with pytest.raises(NotFound):
        add_manual(app, "nope", views=1)


def test_manual_rejects_failed_publication(app):
    app.db.save_publication(
        Publication(id="bad", job_id="j1", clip_id="c02", campaign_id="fast", platform="tiktok",
                    account_id="tt_main", status=PublicationStatus.failed)
    )  # fmt: skip
    with pytest.raises(TrackError):
        add_manual(app, "bad", views=1)


def test_report_aggregates_and_recommendations(app, tmp_path):
    yt = FakeYTStats({"vid0": {"viewCount": 10000, "likeCount": 500, "commentCount": 20},
                      "vid1": {"viewCount": 6000, "likeCount": 30, "commentCount": 1},
                      "vid2": {"viewCount": 100, "likeCount": 1, "commentCount": 0}})  # fmt: skip
    collect_youtube(app, service_factory=lambda acc: yt, clock=lambda: NOW)
    add_manual(app, "j1-c01-tt_main", views=4000, clock=lambda: NOW)

    r = build_report(app)
    rate = Decimal(str(app.settings.campaign("fast").rate_per_1k_views))
    assert r.total_views == 20100
    assert r.total_earnings == (Decimal(20100) / 1000 * rate).quantize(Decimal("0.01"))
    [camp] = r.campaigns
    assert camp.key == "fast" and camp.publications == 4
    accounts = {a.key: a for a in r.accounts}
    assert accounts["yt_main"].views == 16100 and accounts["tt_main"].views == 4000
    assert r.top_clips[0].clip_id == "c01" and r.top_clips[0].platform == "youtube"
    feats = {f.feature: f for f in r.hook_features}
    assert feats["интрига (секрет/никто/ошибка)"].lift > 1
    assert r.score_views_correlation is not None and r.score_views_correlation > 0
    assert "Топ клипов" in render_text(r)

    prompt_file = ROOT / "src" / "clipfactory" / "prompts" / "highlights.md"
    before = prompt_file.read_bytes()
    path = write_prompt_recommendations(app, r, out_dir=tmp_path / "reports")
    text = path.read_text(encoding="utf-8")
    assert "Промпт не изменён" in text and "Никто не говорит" in text
    assert prompt_file.read_bytes() == before  # production-промпт не тронут


def test_hook_features():
    f = hook_features("Почему 90% ошибаются?")
    assert f["вопрос"] and f["число в хуке"] and f["интрига (секрет/никто/ошибка)"]
    assert not hook_features("")["короткий (≤6 слов)"]


def test_bot_stats(app):
    add_manual(app, "j1-c01-tt_main", views=4000, clock=lambda: NOW)
    [reply] = BotController(app).stats()
    assert "4000" in reply.text and reply.text.startswith("<pre>")


def test_strategy_registry():
    camp = type("C", (), {"rate_per_1k_views": 1})()
    assert strategy_for(camp).name == "flat_per_1k"
