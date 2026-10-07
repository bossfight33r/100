"""HTTP-API на TestClient: авторизация, ошибки, весь сценарий десктоп-клиента."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clipfactory.api.app import create_app
from clipfactory.schemas import JobStatus
from tests.conftest import make_synthetic_video, needs_ffmpeg
from tests.helpers import make_fast_app

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def app(tmp_path):
    return make_fast_app(tmp_path)


@pytest.fixture
def client(app):
    return TestClient(create_app(app, TOKEN))


def test_token_required_and_validated(app, client):
    assert client.get("/health").json()["status"] == "ok"  # без токена
    for path in ("/jobs", "/campaigns", "/report", "/accounts", "/capabilities", "/publications"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get(path, headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    assert client.get("/jobs", headers=AUTH).status_code == 200
    assert (
        client.get("/jobs", params={"token": TOKEN}).status_code == 401
    )  # query-токен только для медиа
    with pytest.raises(ValueError):
        create_app(app, "short")


def test_static_lists(client):
    assert {c["id"] for c in client.get("/campaigns", headers=AUTH).json()} >= {"fast"}
    assert {a["id"] for a in client.get("/accounts", headers=AUTH).json()} == {"yt_main", "tt_main"}
    caps = client.get("/capabilities", headers=AUTH).json()
    assert caps["has_ffmpeg"] in (True, False) and "working_encoders" in caps


def test_errors_are_mapped(client):
    assert client.get("/jobs/nope", headers=AUTH).status_code == 404
    assert client.get("/jobs/nope/clips", headers=AUTH).status_code == 404
    r = client.post("/jobs", json={"source": "/x/v.mp4", "campaign_id": "ghost"}, headers=AUTH)
    assert r.status_code == 400 and "unknown campaign" in r.json()["detail"]
    assert client.post("/jobs", json={"campaign_id": "fast"}, headers=AUTH).status_code == 422
    assert client.get("/jobs", params={"status": "bogus"}, headers=AUTH).status_code == 422
    assert client.post("/jobs/nope/cancel", headers=AUTH).status_code == 404


@needs_ffmpeg
@pytest.mark.slow
def test_desktop_client_flow(app, client, tmp_path):
    src = make_synthetic_video(tmp_path / "v.mp4", duration=30)
    r = client.post("/jobs", json={"source": str(src), "campaign_id": "fast"}, headers=AUTH)
    assert r.status_code == 202
    job_id = r.json()["id"]
    # TestClient выполняет фоновую задачу до возврата — pipeline уже прошёл (inline-очередь)
    summary = client.get(f"/jobs/{job_id}", headers=AUTH).json()
    assert summary["job"]["status"] == "awaiting_review"
    assert {s["stage"] for s in summary["stage_runs"]} >= {"ingest", "render"}
    assert [j["id"] for j in client.get("/jobs", headers=AUTH).json()] == [job_id]
    assert client.get("/jobs", params={"status": "failed"}, headers=AUTH).json() == []

    clips = client.get(f"/jobs/{job_id}/clips", headers=AUTH).json()
    assert len(clips) >= 1
    c = clips[0]
    assert c["status"] == "pending_review" and c["meta"] and c["video_url"].endswith("/video")

    # видео: по заголовку и по ?token=, Range для плеера, чужому — 401
    url = c["video_url"]
    assert client.get(url).status_code == 401
    full = client.get(url, headers=AUTH)
    assert full.status_code == 200 and full.headers["content-type"] == "video/mp4"
    assert full.content[4:8] == b"ftyp"
    part = client.get(url, params={"token": TOKEN}, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert (
        client.get(c["thumb_url"], params={"token": TOKEN}).headers["content-type"] == "image/jpeg"
    )

    base = f"/jobs/{job_id}/clips/{c['clip_id']}"
    assert client.post(f"{base}/approve", headers=AUTH).json()["status"] == "approved"
    edited = client.put(f"{base}/metadata", json={"title": "Из десктопа", "platform": "youtube"},
                        headers=AUTH).json()  # fmt: skip
    assert next(m for m in edited["meta"] if m["platform"] == "youtube")["title"] == "Из десктопа"
    assert client.put(f"{base}/metadata", json={}, headers=AUTH).status_code == 400
    assert (
        client.post(f"{base}/rerender-crop", json={"center_x": 1.5}, headers=AUTH).status_code
        == 422
    )

    r = client.post(f"{base}/rerender-crop", json={"center_x": 0.8}, headers=AUTH)
    assert r.status_code == 202
    after = client.get(f"/jobs/{job_id}", headers=AUTH).json()
    assert after["job"]["status"] == "awaiting_review"
    assert (
        client.get(base, headers=AUTH).json()["status"] == "pending_review"
    )  # правка сбросила одобрение
    assert client.post(f"{base}/rerender-captions", headers=AUTH).status_code == 202

    client.post(f"{base}/approve", headers=AUTH)
    pubs = client.post(
        f"/jobs/{job_id}/publish", params={"schedule_only": True}, headers=AUTH
    ).json()
    assert {p["account_id"] for p in pubs} == {"yt_main", "tt_main"}
    assert all(p["status"] == "scheduled" for p in pubs)
    assert len(client.get("/publications", params={"job_id": job_id}, headers=AUTH).json()) == 2

    tt = next(p for p in pubs if p["platform"] == "tiktok")
    r = client.post(f"/publications/{tt['id']}/stats", json={"views": 12000}, headers=AUTH)
    assert r.status_code == 200 and r.json()["views"] == 12000
    assert (
        client.post("/publications/ghost/stats", json={"views": 1}, headers=AUTH).status_code == 404
    )
    rep = client.get("/report", headers=AUTH).json()
    assert rep["total_views"] == 12000 and rep["campaigns"][0]["key"] == "fast"

    # отклонённый клип публиковать нельзя
    client.post(f"{base}/reject", json={"reason": "тест"}, headers=AUTH)
    assert client.get(base, headers=AUTH).json()["status"] == "rejected"


def test_cancel_and_retry_rules(app, client):
    job = app.create_job("/x/v.mp4", "fast")
    app.db.set_job_failed(job.id, None, "boom", "x", True)
    app.db.set_job_status(job.id, JobStatus.published)
    assert client.post(f"/jobs/{job.id}/retry", headers=AUTH).status_code == 400  # нечего повторять
    assert client.post(f"/jobs/{job.id}/cancel", headers=AUTH).status_code == 400  # уже не идёт


# ---------------------------------------------------------------- discover / watch / signals


def _entries(url, limit):
    return [
        {"id": f"v{i}", "ie_key": "Youtube", "title": f"V{i}", "duration": 900,
         "view_count": 1000 * i}
        for i in range(1, 4)
    ][:limit]  # fmt: skip


def test_discover_watch_and_signals_endpoints(app, tmp_path, monkeypatch):
    import numpy as np

    from clipfactory.backends.downloader import DownloadError
    from clipfactory.pipeline import signals
    from clipfactory.schemas import ClipCandidate, Highlights

    client = TestClient(create_app(app, TOKEN, lister=_entries))
    assert client.get("/discover", params={"url": "x"}, headers=AUTH).status_code == 422
    r = client.get("/discover", params={"url": "https://youtube.com/@c"}, headers=AUTH)
    assert [v["title"] for v in r.json()] == ["V3", "V2", "V1"]

    def broken(url, limit):
        raise DownloadError("HTTP 404")

    bad = TestClient(create_app(app, TOKEN, lister=broken))
    r = bad.get("/discover", params={"url": "https://youtube.com/@c"}, headers=AUTH)
    assert r.status_code == 502 and "404" in r.json()["detail"]

    watch = tmp_path / "watch.yaml"
    watch.write_text(
        "sources:\n  - url: https://youtube.com/@c\n    campaign: fast\n    max_new: 1\n",
        encoding="utf-8",
    )
    app.settings.watch_file = watch
    monkeypatch.setattr(app, "enqueue_job", lambda job_id, **kw: None)
    assert client.get("/watch", headers=AUTH).json()[0]["campaign"] == "fast"
    [res] = client.post("/watch/check", headers=AUTH).json()
    assert res["error"] is None and res["queued"][0]["url"].endswith("v3")

    job_id = res["queued"][0]["job_id"]
    assert client.get(f"/jobs/{job_id}/signals", headers=AUTH).status_code == 404
    sig = signals.Signals(hop=0.5, series={"audio": np.r_[np.zeros(10), np.ones(4)]})
    app.storage.put_bytes(
        f"jobs/{job_id}/signals.json", signals.trace(sig).model_dump_json().encode()
    )
    hl = Highlights(candidates=[ClipCandidate(id="c01", start=1, end=6, score=50)])
    app.storage.put_bytes(f"jobs/{job_id}/highlights.json", hl.model_dump_json().encode())
    body = client.get(f"/jobs/{job_id}/signals", headers=AUTH).json()
    assert body["trace"]["series"]["audio"][-1] == 1.0 and body["clips"][0]["id"] == "c01"
    assert client.get("/jobs/nope/signals", headers=AUTH).status_code == 404
