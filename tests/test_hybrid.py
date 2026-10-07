"""Гибридный выбор (ADR-0017): LLM по речи + пики сигналов в промпте и в оценке."""

from __future__ import annotations

import json

import numpy as np
import pytest

from clipfactory.pipeline import signals
from clipfactory.pipeline.select import SelectStage, peak_hint, rescore, select_highlights
from clipfactory.schemas import Campaign, ClipCandidate
from tests.helpers import make_app
from tests.test_select import make_transcript


def spike_signals(length_sec: float, at: list[float], hop: float = 0.5) -> signals.Signals:
    x = np.zeros(int(length_sec / hop))
    for t in at:
        x[int(t / hop) : int((t + 2) / hop)] = 1.0
    return signals.Signals(hop=hop, series={"audio": x})


def test_peaks_in_range_with_gap():
    sig = spike_signals(300, [50, 55, 200])
    assert [round(t) for t, _, _ in signals.peaks(sig)] == [50, 200]  # 55 ближе 20 с к 50
    assert [round(t) for t, _, _ in signals.peaks(sig, 100, 300)] == [200]
    assert signals.peaks(sig, 0, 300)[0][2] == "audio"
    assert signals.peaks(spike_signals(100, []), 0, 100) == []


def test_window_strength():
    sig = spike_signals(300, [100])
    assert signals.window_strength(sig, 95, 110) == pytest.approx(0.7 + 0.3 * 4 / 30, abs=0.02)
    assert signals.window_strength(sig, 0, 50) == 0


def test_peak_hint_and_rescore():
    sig = spike_signals(300, [100])
    hint = peak_hint(sig, 0, 300)
    assert "100.2s strength 1.00 (audio)" in hint
    assert peak_hint(sig, 150, 300) == ""
    base = ClipCandidate(id="x", start=90, end=110, score=60)
    assert rescore(base, sig).score > 60
    assert rescore(base.model_copy(update={"start": 10, "end": 30}), sig).score == 42


class RecordingLLM:
    """Два кандидата равной оценки; запоминает промпты."""

    def __init__(self):
        self.prompts = []

    def complete(self, system, prompt, max_tokens=1000):
        self.prompts.append(prompt)
        return json.dumps(
            {
                "highlights": [
                    {"title": "a", "start_time": 20, "end_time": 35, "score": 70,
                     "hook_sentence": "", "virality_reason": ""},
                    {"title": "b", "start_time": 120, "end_time": 135, "score": 70,
                     "hook_sentence": "", "virality_reason": ""},
                ]
            }
        )  # fmt: skip


def test_hybrid_signal_decides_between_equal_llm_scores():
    transcript = make_transcript(n_sentences=40)
    campaign = Campaign(
        id="c", name="c", rate_per_1k_views=1, platforms=["youtube"],
        clip_min_sec=10, clip_max_sec=20, clip_count=1, selection="hybrid",
    )  # fmt: skip
    llm = RecordingLLM()
    sig = spike_signals(transcript.duration, [128])
    [clip] = select_highlights(transcript, campaign, llm, chunk_sec=1200, overlap_sec=60, sig=sig)
    assert clip.start > 100
    assert "engagement peaks" in llm.prompts[0] and "128." in llm.prompts[0]
    plain = RecordingLLM()
    select_highlights(transcript, campaign, plain, chunk_sec=1200, overlap_sec=60)
    assert "engagement peaks" not in plain.prompts[0]


def test_hybrid_campaign_requires_transcribe():
    with pytest.raises(ValueError):
        Campaign(
            id="c", name="c", rate_per_1k_views=1, platforms=["youtube"],
            selection="hybrid", transcribe=False,
        )  # fmt: skip


def test_hybrid_config_and_inputs(tmp_path, synthetic_video):
    cdir = tmp_path / "campaigns"
    cdir.mkdir()
    (cdir / "h.yaml").write_text(
        "id: h\nname: H\nrate_per_1k_views: 1\nplatforms: [youtube]\nselection: hybrid\n",
        encoding="utf-8",
    )
    app = make_app(tmp_path, campaigns_dir=cdir)
    ctx = app.context(app.create_job(str(synthetic_video), "h"))
    cfg = SelectStage().config(ctx)
    assert cfg["selection"] == "hybrid" and "prompt_sha" in cfg
    assert SelectStage().input_keys(ctx) == [ctx.key("transcript.json"), ctx.key("audio.wav")]


@pytest.mark.slow
def test_hybrid_job_end_to_end(tmp_path, long_synthetic_video):
    from clipfactory.schemas import JobStatus, SignalsTrace

    cdir = tmp_path / "campaigns"
    cdir.mkdir()
    (cdir / "h.yaml").write_text(
        "id: h\nname: H\nrate_per_1k_views: 1\nplatforms: [youtube]\nselection: hybrid\n"
        "clip_min_sec: 20\nclip_max_sec: 30\nclip_count: 1\n",
        encoding="utf-8",
    )
    app = make_app(tmp_path, campaigns_dir=cdir)
    job = app.run_job(app.create_job(str(long_synthetic_video), "h").id)
    assert job.status is JobStatus.awaiting_review, job.error_message
    ctx = app.context(job)
    assert ctx.read_model(ctx.key("signals.json"), SignalsTrace).series["audio"]
