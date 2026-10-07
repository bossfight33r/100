"""cf discover: видео канала/плейлиста без скачивания (сеть подменена)."""

from __future__ import annotations

import json

import pytest

from clipfactory.backends.downloader import DownloadError
from clipfactory.backends.downloader.discover import flatten_entries
from clipfactory.discover import discover, entry_url

CHANNEL = {
    "id": "UCxyz",
    "entries": [
        {  # вкладка Videos
            "id": "videos",
            "entries": [
                {"id": "a1", "ie_key": "Youtube", "title": "Мейджор финал", "duration": 3600,
                 "view_count": 900_000, "channel": "ESL"},
                {"id": "b2", "url": "https://www.youtube.com/watch?v=b2", "title": "Клатч 1v5",
                 "duration": 900, "view_count": 2_000_000},
                {"id": "c3", "ie_key": "Youtube", "title": "Короткое", "duration": 40,
                 "view_count": 5_000_000},
                {"id": "d4", "ie_key": "Youtube", "title": "Эфир", "live_status": "is_live",
                 "view_count": 10},
                {"id": "e5", "ie_key": "Youtube", "title": "Без длительности", "view_count": 50},
            ],
        },
        {"id": "f6", "url": "ftp://evil/x", "title": "не http"},
    ],
}  # fmt: skip


def lister(entries=CHANNEL):
    calls = []

    def _list(url, limit):
        calls.append((url, limit))
        return flatten_entries(entries)[:limit]

    _list.calls = calls
    return _list


def test_flatten_nested_tabs():
    assert [e["id"] for e in flatten_entries(CHANNEL)] == ["a1", "b2", "c3", "d4", "e5", "f6"]
    assert flatten_entries({"id": "single", "title": "x"}) == [{"id": "single", "title": "x"}]
    assert flatten_entries(None) == []


def test_entry_url_variants():
    assert entry_url({"id": "a1", "ie_key": "Youtube"}) == "https://www.youtube.com/watch?v=a1"
    assert entry_url({"url": "https://x.example/v"}) == "https://x.example/v"
    assert entry_url({"url": "file:///etc/passwd"}) is None
    assert entry_url({"id": "../../x", "ie_key": "Youtube"}).endswith("..%2F..%2Fx")
    assert entry_url({"id": "z", "ie_key": "Twitch"}) is None


def test_discover_filters_and_ranks():
    found = discover("https://youtube.com/@esl", lister=lister(), min_sec=300, max_sec=7200)
    assert [c.title for c in found] == ["Клатч 1v5", "Мейджор финал", "Без длительности"]
    assert found[1].channel == "ESL" and found[1].heatmap_points is None


def test_discover_skips_processed_unless_asked():
    seen = {"https://www.youtube.com/watch?v=b2"}
    found = discover("u", lister=lister(), seen=seen)
    assert "Клатч 1v5" not in [c.title for c in found]
    found = discover("u", lister=lister(), seen=seen, include_processed=True)
    assert next(c for c in found if c.title == "Клатч 1v5").already_processed


def test_discover_heatmap_puts_videos_with_curve_first():
    def prober(url):
        if url.endswith("a1"):
            return {"heatmap": [{"start_time": 0, "end_time": 5, "value": 1.0}] * 3}
        if url.endswith("e5"):
            return {"duration": 30}  # длительность выяснилась — короче минимума
        raise DownloadError("private video")

    found = discover("u", lister=lister(), prober=prober)
    assert [(c.title, c.heatmap_points) for c in found] == [
        ("Мейджор финал", 3),
        ("Клатч 1v5", None),  # проба не удалась — остаётся без отметки
    ]


def test_cli_discover_json_and_enqueue(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from clipfactory.backends.downloader import discover as backend
    from clipfactory.cli import app as cli
    from tests.helpers import ROOT

    monkeypatch.setattr(backend, "list_videos_ytdlp", lister())
    monkeypatch.setenv("CF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CF_CAMPAIGNS_DIR", str(ROOT / "config" / "campaigns"))
    monkeypatch.setenv("CF_LLM_PROVIDER", "fake")
    monkeypatch.setenv("CF_QUEUE", "rq")  # постановка без выполнения
    monkeypatch.setenv("CF_REDIS_URL", "redis://127.0.0.1:1/0")

    enqueued = []
    monkeypatch.setattr(
        "clipfactory.services.App.enqueue_job", lambda self, job_id, **kw: enqueued.append(job_id)
    )
    res = CliRunner().invoke(
        cli, ["--json", "discover", "https://youtube.com/@esl", "--enqueue", "2", "-c", "cs2"]
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert [v["title"] for v in data["videos"]][:2] == ["Клатч 1v5", "Мейджор финал"]
    assert [q["url"] for q in data["queued"]] == [
        "https://www.youtube.com/watch?v=b2",
        "https://www.youtube.com/watch?v=a1",
    ]
    assert len(enqueued) == 2

    # повторный поиск не предлагает уже поставленное
    res = CliRunner().invoke(cli, ["--json", "discover", "https://youtube.com/@esl"])
    titles = [v["title"] for v in json.loads(res.output)["videos"]]
    assert titles == ["Без длительности"]


def test_cli_discover_enqueue_requires_campaign():
    from typer.testing import CliRunner

    from clipfactory.cli import app as cli

    res = CliRunner().invoke(cli, ["discover", "u", "--enqueue", "1"])
    assert res.exit_code == 1 and "--campaign" in res.output


@pytest.mark.parametrize("limit", [1, 3])
def test_limit_passed_to_lister(limit):
    fake = lister()
    discover("u", lister=fake, limit=limit, min_sec=0, max_sec=10**6)
    assert fake.calls == [("u", limit)]
