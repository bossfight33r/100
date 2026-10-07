"""Выбор моментов по сигналам без речи (ADR-0014)."""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from clipfactory.backends.downloader import SOURCE_INFO_NAME, source_info_from_ytdlp
from clipfactory.config import ConfigError
from clipfactory.media import ffmpeg
from clipfactory.pipeline import signals
from clipfactory.pipeline.ingest import IngestStage
from clipfactory.pipeline.orchestrator import Orchestrator, default_stages
from clipfactory.schemas import (
    Campaign,
    Highlights,
    JobStatus,
    SelectionMode,
    SourceInfo,
    Transcript,
    Word,
)
from clipfactory.services import App, llm_config_problem
from tests.conftest import needs_ffmpeg
from tests.helpers import make_app, make_settings

RATE = 16000


def write_wav(path: Path, duration: float, bursts: list[tuple[float, float]]) -> Path:
    """Тихий шум (-46 dBFS) и громкие всплески (-10 dBFS) в заданных интервалах."""
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.005, int(duration * RATE))
    for a, b in bursts:
        x[int(a * RATE) : int(b * RATE)] = rng.normal(0, 0.3, int(b * RATE) - int(a * RATE))
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return path


def campaign(**kw) -> Campaign:
    values = dict(
        id="cs",
        name="CS",
        rate_per_1k_views=1,
        platforms=["youtube"],
        clip_min_sec=10,
        clip_max_sec=30,
        clip_count=2,
        selection="signals",
        transcribe=False,
    )
    values.update(kw)
    return Campaign.model_validate(values)


# ---------------------------------------------------------------- source info


def test_source_info_keeps_only_valid_points():
    info = source_info_from_ytdlp(
        {
            "title": "Major final",
            "uploader": "ESL",
            "duration": 600,
            "view_count": "12345",
            "heatmap": [
                {"start_time": 0, "end_time": 6, "value": 0.2},
                {"start_time": 6, "end_time": 12, "value": 1.7},  # обрезается до 1
                {"start_time": 12, "end_time": 12, "value": 0.5},  # пустой интервал
                {"start_time": "x", "end_time": 20, "value": 0.5},
                "garbage",
            ],
            "chapters": [{"start_time": 0, "end_time": 300, "title": "Mirage"}, None],
        }
    )
    parsed = SourceInfo.model_validate(info)
    assert parsed.channel == "ESL" and parsed.view_count == 12345
    assert [p.value for p in parsed.heatmap] == [0.2, 1.0]
    assert [c.title for c in parsed.chapters] == ["Mirage"]


def test_source_info_without_heatmap():
    parsed = SourceInfo.model_validate(source_info_from_ytdlp({"title": "x", "heatmap": None}))
    assert parsed.heatmap == [] and parsed.duration is None


# ---------------------------------------------------------------- series


def test_rms_db_levels(tmp_path):
    wav = write_wav(tmp_path / "a.wav", 10, [(4, 6)])
    db = signals.rms_db(wav)
    assert len(db) == 20
    assert db[2] < -40 and db[10] > -15


def test_audio_scores_peak_on_burst(tmp_path):
    wav = write_wav(tmp_path / "a.wav", 120, [(70, 73)])
    scores = signals.audio_scores(signals.rms_db(wav))
    peak_t = (int(np.argmax(scores)) + 0.5) * signals.HOP
    assert 69 <= peak_t <= 74
    assert scores[:100].max() == 0  # ровный фон не даёт оценки


def test_audio_scores_ignore_silence():
    db = np.full(200, -100.0)
    db[100:104] = -80.0  # громче фона, но всё ещё тишина
    assert signals.audio_scores(db).max() == 0


def test_heatmap_normalization_and_flat_curve():
    info = SourceInfo(
        heatmap=[
            {"start_time": 0, "end_time": 10, "value": 0.3},
            {"start_time": 10, "end_time": 20, "value": 1.0},
            {"start_time": 20, "end_time": 30, "value": 0.3},
        ]
    )
    hm = signals.heatmap_scores(info, 60)
    assert hm[25] == pytest.approx(1.0) and hm[5] == 0
    flat = SourceInfo(heatmap=[{"start_time": 0, "end_time": 30, "value": 0.5}])
    assert signals.heatmap_scores(flat, 60) is None
    assert signals.heatmap_scores(SourceInfo(), 60) is None


def test_fused_weights_only_available_signals():
    sig = signals.Signals(hop=0.5, series={"audio": np.array([0.0, 1.0])})
    assert list(sig.fused()) == [0.0, 1.0]  # без heatmap аудио весит 100%


# ---------------------------------------------------------------- windows


def test_pick_windows_places_peak_late_in_clip(tmp_path):
    wav = write_wav(tmp_path / "a.wav", 300, [(80, 83), (200, 202)])
    sig = signals.build_signals(wav, None)
    picked = signals.pick_windows(sig, campaign(), 300)
    assert len(picked) == 2
    target = signals.target_duration(campaign())
    for c, burst in zip(sorted(picked, key=lambda c: c.start), (80, 200), strict=True):
        assert c.duration == pytest.approx(target, abs=0.01)
        assert c.start < burst < c.end
        assert (burst - c.start) / c.duration > 0.4  # завязка перед пиком
        assert "audio" in c.reason


def test_pick_windows_heatmap_wins_over_flat_audio(tmp_path):
    wav = write_wav(tmp_path / "a.wav", 300, [])
    info = SourceInfo(
        title="Финал мейджора",
        heatmap=[
            {"start_time": t, "end_time": t + 3, "value": 0.9 if t == 150 else 0.2}
            for t in range(0, 300, 3)
        ],
        chapters=[{"start_time": 120, "end_time": 240, "title": "Клатч 1v4"}],
    )
    sig = signals.build_signals(wav, info)
    picked = signals.pick_windows(sig, campaign(), 300, info=info)
    assert picked and picked[0].start < 151 < picked[0].end
    assert picked[0].hook == "Клатч 1v4" and "Финал мейджора" in picked[0].reason


def test_pick_windows_quiet_video_gives_nothing(tmp_path):
    sig = signals.build_signals(write_wav(tmp_path / "a.wav", 120, []), None)
    assert signals.pick_windows(sig, campaign(), 120) == []


def test_pick_windows_video_shorter_than_min_clip(tmp_path):
    sig = signals.build_signals(write_wav(tmp_path / "a.wav", 8, [(4, 5)]), None)
    assert signals.pick_windows(sig, campaign(), 8) == []


def test_snap_to_words_does_not_cut_words():
    words = [Word(text="клатч", start=9.8, end=10.4), Word(text="есть", start=19.9, end=20.5)]
    assert signals.snap_to_words(10.0, 20.0, words) == pytest.approx((9.7, 20.6))
    assert signals.snap_to_words(5.0, 15.0, words) == (5.0, 15.0)


# ---------------------------------------------------------------- config


def test_campaign_transcribe_false_requires_signals():
    with pytest.raises(ValueError, match="selection: signals"):
        campaign(selection="transcript")
    assert campaign().selection is SelectionMode.signals


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        (dict(llm_provider="anthropic", anthropic_api_key=None), "ANTHROPIC_API_KEY"),
        (dict(llm_provider="openai_compat", llm_base_url=None), "CF_LLM_BASE_URL"),
        (
            dict(llm_provider="openai_compat", llm_base_url="https://api.example.com/v1"),
            "CF_LLM_API_KEY",
        ),
        (dict(llm_provider="openai_compat", llm_base_url="http://localhost:1234/v1"), None),
        (
            dict(
                llm_provider="openai_compat",
                llm_base_url="https://api.example.com/v1",
                llm_api_key="k" * 20,
            ),
            None,
        ),
        (dict(llm_provider="anthropic", anthropic_api_key="k" * 20), None),
        (dict(llm_provider="ollama"), None),
    ],
)
def test_llm_config_problem(tmp_path, overrides, problem):
    result = llm_config_problem(make_settings(tmp_path, **overrides))
    if problem is None:
        assert result is None
    else:
        assert problem in result
        assert "k" * 20 not in result


def test_create_job_fails_fast_without_llm_key(tmp_path, synthetic_video):
    app = App(make_settings(tmp_path, llm_provider="anthropic", anthropic_api_key=None))
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        app.create_job(str(synthetic_video), "example")
    assert app.db.list_jobs() == []


# ---------------------------------------------------------------- pipeline


CS_CAMPAIGN = """
id: cs
name: CS2 highlights
rate_per_1k_views: 1
platforms: [youtube]
clip_min_sec: 8
clip_max_sec: 16
clip_count: 1
selection: signals
transcribe: false
"""


class NoWhisper:
    def transcribe(self, *a, **kw):  # pragma: no cover - не должен вызываться
        raise AssertionError("Whisper must not run for transcribe: false")


def make_loud_video(path: Path, duration: float, loud: tuple[float, float]) -> Path:
    a, b = loud
    ffmpeg.ffmpeg(
        ["-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=15:duration={duration}",
         "-f", "lavfi", "-i", f"anoisesrc=color=pink:sample_rate=48000:duration={duration}",
         "-af", f"volume='if(between(t,{a},{b}),0.9,0.02)':eval=frame",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        timeout=120,
    )  # fmt: skip
    return path


@needs_ffmpeg
@pytest.mark.slow
def test_signals_job_end_to_end_without_whisper(tmp_path):
    src = make_loud_video(tmp_path / "cs.mp4", 60, (40, 43))
    cdir = tmp_path / "campaigns"
    cdir.mkdir()
    (cdir / "cs.yaml").write_text(CS_CAMPAIGN, encoding="utf-8")

    def fake_ytdlp(url, dest):
        info = {
            "title": "CS2 stream",
            "heatmap": [
                {"start_time": t, "end_time": t + 2, "value": 0.1} for t in range(0, 60, 2)
            ],
        }
        (dest / SOURCE_INFO_NAME).write_text(
            json.dumps(source_info_from_ytdlp(info)), encoding="utf-8"
        )
        return src

    app = make_app(tmp_path, transcriber=NoWhisper(), campaigns_dir=cdir)
    job = app.create_job("https://www.youtube.com/watch?v=abc", "cs")
    ctx = app.context(job)
    stages = default_stages()
    stages[0] = IngestStage(ytdlp_downloader=fake_ytdlp)
    Orchestrator(app.db, stages).run(ctx)
    assert ctx.storage.exists(ctx.key("source.info.json"))
    final = app.db.get_job(job.id)
    assert final.status == JobStatus.awaiting_review, final.error_message
    transcript = ctx.read_model(ctx.key("transcript.json"), Transcript)
    assert transcript.words == [] and transcript.model == "none"
    (cand,) = ctx.read_model(ctx.key("highlights.json"), Highlights).candidates
    assert cand.start < 41.5 < cand.end
    assert 8 <= cand.duration <= 16
    assert ctx.storage.exists(ctx.clip_key("c01", "final.mp4"))


def test_default_campaign_cache_keys_unchanged(tmp_path, synthetic_video):
    """Обновление не должно инвалидировать кеш transcribe/select у уже идущих job."""
    from clipfactory.pipeline.select import SelectStage
    from clipfactory.pipeline.transcribe import TranscribeStage

    app = make_app(tmp_path)
    ctx = app.context(app.create_job(str(synthetic_video), "example"))
    assert set(TranscribeStage().config(ctx)) == {"backend", "language"}
    assert set(SelectStage().config(ctx)) == {
        "llm", "prompt_sha", "clip_min_sec", "clip_max_sec", "clip_count", "notes", "chunk",
    }  # fmt: skip
    assert SelectStage().input_keys(ctx) == [ctx.key("transcript.json")]


@needs_ffmpeg
def test_ingest_drops_stale_source_info(tmp_path, synthetic_video):
    app = make_app(tmp_path)
    ctx = app.context(app.create_job(str(synthetic_video), "example"))
    ctx.storage.put_bytes(ctx.key("source.info.json"), b"{}")
    res = IngestStage().run(ctx)
    assert res.outputs == [ctx.key("source.mp4")]
    assert not ctx.storage.exists(ctx.key("source.info.json"))
