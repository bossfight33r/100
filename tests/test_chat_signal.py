"""Сигнал чата записи стрима (ADR-0016)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from clipfactory.backends.downloader import SOURCE_INFO_NAME, DownloadError
from clipfactory.backends.downloader.live_chat import parse_live_chat
from clipfactory.pipeline import signals
from clipfactory.pipeline.ingest import IngestStage
from clipfactory.schemas import ChatActivity
from tests.conftest import needs_ffmpeg
from tests.helpers import ROOT, make_app


def chat_line(offset_ms: int, messages: int = 1, other: int = 0) -> str:
    actions = [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {}}}}] * messages
    actions += [{"addLiveChatTickerItemAction": {}}] * other
    return json.dumps(
        {"replayChatItemAction": {"actions": actions, "videoOffsetTimeMsec": str(offset_ms)}}
    )


def test_parse_live_chat_counts_messages_per_second():
    lines = [
        chat_line(500),
        chat_line(900, messages=2, other=3),  # тикер не сообщение
        chat_line(2500),
        chat_line(99_000),  # за пределами видео
        chat_line(-1000),
        "not json",
        json.dumps({"something": "else"}),
        json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": "abc"}}),
    ]
    assert parse_live_chat(lines, duration=4) == [3, 0, 1, 0]


def burst_chat(duration: int, burst_at: int, background: int = 1, burst: int = 30) -> ChatActivity:
    counts = [background] * duration
    for t in range(burst_at, burst_at + 5):
        counts[t] = burst
    return ChatActivity(hop=1.0, counts=counts)


def test_chat_scores_shifted_back_by_reaction_lag():
    chat = burst_chat(600, burst_at=306)  # чат взорвался через ~6 с после момента в 300 с
    scores = signals.chat_scores(chat, length=1200)
    peak_t = (int(np.argmax(scores)) + 0.5) * signals.HOP
    assert 297 <= peak_t <= 308
    assert scores[:400].max() == 0  # ровный фон


def test_chat_scores_sparse_chat_ignored():
    assert signals.chat_scores(ChatActivity(hop=1.0, counts=[0] * 100 + [10]), 300) is None


def test_build_signals_includes_chat(tmp_path):
    sig = signals.build_signals(None, None, chat=burst_chat(120, 60))
    assert sig.length == 0  # без аудио и длительности ряда нет
    from tests.test_signals import write_wav

    wav = write_wav(tmp_path / "a.wav", 120, [])
    sig = signals.build_signals(wav, None, chat=burst_chat(120, 60))
    assert set(sig.series) == {"audio", "chat"}


# ---------------------------------------------------------------- ingest

SIGNALS_CAMPAIGN = """
id: stream
name: Stream
rate_per_1k_views: 1
platforms: [youtube]
clip_min_sec: 8
clip_max_sec: 16
clip_count: 1
selection: signals
transcribe: false
"""


def _app(tmp_path):
    cdir = tmp_path / "campaigns"
    cdir.mkdir(exist_ok=True)
    (cdir / "stream.yaml").write_text(SIGNALS_CAMPAIGN, encoding="utf-8")
    return make_app(tmp_path, campaigns_dir=cdir)


def _ytdlp(src: Path, was_live: bool = True):
    def fake(url, dest):
        (dest / SOURCE_INFO_NAME).write_text(
            json.dumps({"title": "stream", "was_live": was_live}), encoding="utf-8"
        )
        return src

    return fake


@needs_ffmpeg
def test_ingest_stores_chat_for_signals_campaign(tmp_path, synthetic_video):
    def fetch(url, dest):
        path = dest / "chat.live_chat.json"
        path.write_text("\n".join(chat_line(i * 100) for i in range(60)), encoding="utf-8")
        return path

    app = _app(tmp_path)
    ctx = app.context(app.create_job("https://www.youtube.com/watch?v=s", "stream"))
    stage = IngestStage(ytdlp_downloader=_ytdlp(Path(synthetic_video)), chat_fetcher=fetch)
    res = stage.run(ctx)
    assert ctx.key("chat.json") in res.outputs
    chat = ctx.read_model(ctx.key("chat.json"), ChatActivity)
    assert sum(chat.counts) == 60 and chat.counts[0] == 10
    assert stage.config(ctx) == {"source": ctx.job.source, "chat": True}


@needs_ffmpeg
@pytest.mark.parametrize("case", ["not_live", "fetch_fails", "no_chat", "transcript_campaign"])
def test_ingest_without_chat_still_succeeds(tmp_path, synthetic_video, case):
    def fetch(url, dest):
        if case in ("not_live", "transcript_campaign"):
            raise AssertionError("chat must not be fetched here")
        if case == "fetch_fails":
            raise DownloadError("chat disabled")
        return None

    (tmp_path / "campaigns").mkdir()
    (tmp_path / "campaigns" / "example.yaml").write_bytes(
        (ROOT / "config" / "campaigns" / "example.yaml").read_bytes()
    )
    app = _app(tmp_path)
    campaign = "example" if case == "transcript_campaign" else "stream"
    ctx = app.context(app.create_job("https://www.youtube.com/watch?v=s", campaign))
    stage = IngestStage(
        ytdlp_downloader=_ytdlp(Path(synthetic_video), was_live=case != "not_live"),
        chat_fetcher=fetch,
    )
    ctx.storage.put_bytes(ctx.key("chat.json"), b"{}")  # от прошлой загрузки
    res = stage.run(ctx)
    assert ctx.key("chat.json") not in res.outputs
    assert not ctx.storage.exists(ctx.key("chat.json"))
    assert ctx.key("source.mp4") in res.outputs


# ---------------------------------------------------------------- trace / cf signals


def test_trace_downsamples_by_max():
    sig = signals.Signals(hop=0.5, series={"audio": np.array([0.1, 0.9, 0.0, 0.2, 0.5])})
    tr = signals.trace(sig, step=1.0)
    assert tr.hop == 1.0 and tr.series["audio"] == [0.9, 0.2, 0.5]
    assert tr.fused == [0.9, 0.2, 0.5] and tr.weights == {"audio": 0.4}


def test_cli_signals_renders_trace(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from clipfactory.cli import app as cli
    from clipfactory.cli import sparkline
    from clipfactory.schemas import ClipCandidate, Highlights

    assert sparkline([0, 0.5, 1.0], 3) == " ▄█"
    app = make_app(tmp_path)
    sig = signals.Signals(
        hop=0.5,
        series={"audio": np.r_[np.zeros(100), np.ones(10), np.zeros(90)], "chat": np.zeros(200)},
    )
    app.storage.put_bytes("jobs/j1/signals.json", signals.trace(sig).model_dump_json().encode())
    hl = Highlights(candidates=[ClipCandidate(id="c01", start=45, end=60, score=90, reason="r")])
    app.storage.put_bytes("jobs/j1/highlights.json", hl.model_dump_json().encode())
    monkeypatch.setenv("CF_DATA_DIR", str(app.settings.data_dir))
    res = CliRunner().invoke(cli, ["signals", "j1", "--width", "50"])
    assert res.exit_code == 0, res.output
    assert "audio ×0.4" in res.output and "итог" in res.output and "c01  0:45–1:00" in res.output
    assert "█" in res.output and "1━━" in res.output
    res = CliRunner().invoke(cli, ["signals", "missing"])
    assert res.exit_code == 1
