"""cf watch: автоматическая постановка новых видео каналов (сеть подменена)."""

from __future__ import annotations

import json

import pytest

from clipfactory.config import ConfigError, load_watch
from clipfactory.schemas import WatchSource
from clipfactory.watch import check_all, check_source
from tests.helpers import ROOT, make_app


def entries(n=5):
    return [
        {"id": f"v{i}", "ie_key": "Youtube", "title": f"Видео {i}",
         "duration": 1200, "view_count": 10_000 * i}
        for i in range(1, n + 1)
    ]  # fmt: skip


def app_with_enqueue(tmp_path, monkeypatch):
    app = make_app(tmp_path, campaigns_dir=ROOT / "config" / "campaigns")
    queued = []
    monkeypatch.setattr(app, "enqueue_job", lambda job_id, **kw: queued.append(job_id))
    return app, queued


def test_watch_queues_new_videos_once(tmp_path, monkeypatch):
    app, queued = app_with_enqueue(tmp_path, monkeypatch)
    src = WatchSource(
        url="https://youtube.com/@c/videos", campaign="cs2", min_views=20_000, max_new=2
    )
    r = check_source(app, src, lambda url, limit: entries())
    assert r.error is None
    assert [u for _, u in r.queued] == [
        "https://www.youtube.com/watch?v=v5",
        "https://www.youtube.com/watch?v=v4",
    ]
    assert len(queued) == 2
    r = check_source(app, src, lambda url, limit: entries())
    assert [u.rsplit("=", 1)[1] for _, u in r.queued] == ["v3", "v2"]
    r = check_source(app, src, lambda url, limit: entries())
    assert r.queued == []  # v1 ниже min_views


def test_watch_errors_do_not_stop_other_sources(tmp_path, monkeypatch):
    app, queued = app_with_enqueue(tmp_path, monkeypatch)

    def lister(url, limit):
        if "broken" in url:
            raise RuntimeError("HTTP 500")
        return entries(1)

    results = check_all(
        app,
        [
            WatchSource(url="https://x.example/broken", campaign="cs2"),
            WatchSource(url="https://x.example/ok", campaign="nope"),
            WatchSource(url="https://x.example/ok", campaign="cs2"),
        ],
        lister,
    )
    assert "HTTP 500" in results[0].error
    assert "unknown campaign" in results[1].error
    assert results[2].error is None and len(results[2].queued) == 1


def test_watch_job_failure_is_not_source_failure(tmp_path, monkeypatch):
    app, _ = app_with_enqueue(tmp_path, monkeypatch)

    def boom(job_id, **kw):
        raise RuntimeError("render failed")

    monkeypatch.setattr(app, "enqueue_job", boom)
    r = check_source(
        app, WatchSource(url="https://x.example/c", campaign="cs2"), lambda u, n: entries(1)
    )
    assert r.error is None and len(r.queued) == 1


def test_load_watch(tmp_path):
    assert load_watch(tmp_path / "missing.yaml") == []
    assert load_watch(ROOT / "config" / "watch.example.yaml")[0].campaign == "cs2"
    bad = tmp_path / "w.yaml"
    bad.write_text("sources:\n  - url: file:///etc/passwd\n    campaign: cs2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="http"):
        load_watch(bad)


def test_cli_watch_once(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from clipfactory.backends.downloader import discover as backend
    from clipfactory.cli import app as cli

    watch_file = tmp_path / "watch.yaml"
    watch_file.write_text(
        "sources:\n  - url: https://youtube.com/@c/videos\n    campaign: cs2\n    max_new: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(backend, "list_videos_ytdlp", lambda url, limit: entries())
    monkeypatch.setattr("clipfactory.services.App.enqueue_job", lambda self, job_id, **kw: None)
    monkeypatch.setenv("CF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CF_CAMPAIGNS_DIR", str(ROOT / "config" / "campaigns"))
    monkeypatch.setenv("CF_WATCH_FILE", str(watch_file))
    monkeypatch.setenv("CF_LLM_PROVIDER", "fake")
    res = CliRunner().invoke(cli, ["--json", "watch"])
    assert res.exit_code == 0, res.output
    [r] = json.loads(res.output)
    assert r["error"] is None and r["queued"][0][1].endswith("v5")

    monkeypatch.setenv("CF_WATCH_FILE", str(tmp_path / "none.yaml"))
    res = CliRunner().invoke(cli, ["watch"])
    assert res.exit_code == 1 and "watch.example.yaml" in res.output
