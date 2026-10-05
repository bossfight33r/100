import json

import pytest

from clipfactory.backends.llm.base import LLMError
from clipfactory.backends.llm.fake import FakeLLM
from clipfactory.pipeline.select import (
    RawHighlight,
    build_chunks,
    dedupe,
    refine,
    sanitize,
    select_highlights,
)
from clipfactory.schemas import Campaign, ClipCandidate, Segment, Transcript, Word


def make_transcript(n_sentences: int = 40, words_per: int = 8) -> Transcript:
    """Предложения по 8 слов (0.4с + 0.1 зазор), между предложениями пауза 0.8с."""
    words, segments, t = [], [], 0.0
    for s in range(n_sentences):
        seg_words = []
        for k in range(words_per):
            text = f"w{s}_{k}" + ("." if k == words_per - 1 else "")
            seg_words.append(Word(text=text, start=round(t, 3), end=round(t + 0.4, 3)))
            t += 0.5
        t += 0.8
        words += seg_words
        segments.append(
            Segment(start=seg_words[0].start, end=seg_words[-1].end,
                    text=" ".join(w.text for w in seg_words), words=seg_words)
        )  # fmt: skip
    return Transcript(
        language="ru", model="t", duration=round(t, 3), words=words, segments=segments
    )


CAMPAIGN = Campaign(
    id="c", name="c", rate_per_1k_views=1, platforms=["youtube"],
    clip_min_sec=10, clip_max_sec=20, clip_count=2,
)  # fmt: skip


def test_chunks_cover_with_overlap_and_absolute_times():
    tr = make_transcript(200)  # ~960 сек
    chunks = build_chunks(tr, chunk_sec=300, overlap_sec=60)
    assert chunks[0].start == 0 and chunks[1].start == 240
    assert chunks[-1].end == pytest.approx(tr.duration)
    # таймстемпы во втором чанке — абсолютные, не от начала чанка (баг оригинала)
    first_ts = float(chunks[1].text.split("]")[0].strip("[").split("-")[0])
    assert first_ts >= 230


def test_sanitize_drops_garbage_and_clamps():
    raw = {"highlights": [
        {"start_time": "5", "end_time": 15, "score": 150},
        {"start_time": 30, "end_time": 20},
        {"start_time": "nan", "end_time": 10},
        "junk",
        {"start_time": -5, "end_time": 500, "score": 50},
    ]}  # fmt: skip
    out = sanitize(raw, lo=0, hi=100)
    assert [(h.start, h.end, h.score) for h in out] == [(5, 15, 100), (0, 100, 50)]


def test_refine_snaps_to_word_boundaries_and_pauses():
    tr = make_transcript(10)
    # LLM дала границы посреди слов
    raw = [RawHighlight(start=6.75, end=19.3, score=80, title="t", hook="h", reason="r")]
    [c] = refine(raw, tr, CAMPAIGN)
    starts = {w.start for w in tr.words}
    ends = {w.end for w in tr.words}
    # начало — на слове после паузы (начало предложения), с небольшим отступом
    first = min((w for w in tr.words if w.start >= c.start), key=lambda w: w.start)
    assert first.start in starts and first.text.startswith("w")
    assert first.text.endswith("_0")
    last = max((w for w in tr.words if w.end <= c.end), key=lambda w: w.end)
    assert last.end in ends and last.text.endswith(".")
    assert CAMPAIGN.clip_min_sec <= c.duration <= CAMPAIGN.clip_max_sec
    # не режем внутри слова
    for w in tr.words:
        assert not (w.start < c.start < w.end) and not (w.start < c.end < w.end)


def test_refine_enforces_max_and_min():
    tr = make_transcript(20)
    raw = [
        RawHighlight(start=0.0, end=80.0, score=90, title="", hook="", reason=""),
        RawHighlight(start=40.0, end=42.0, score=70, title="", hook="", reason=""),
    ]
    out = refine(raw, tr, CAMPAIGN)
    assert len(out) == 2
    for c in out:
        assert CAMPAIGN.clip_min_sec <= c.duration <= CAMPAIGN.clip_max_sec


def test_dedupe_keeps_higher_score():
    a = ClipCandidate(id="a", start=0, end=20, score=50)
    b = ClipCandidate(id="b", start=5, end=25, score=90)
    c = ClipCandidate(id="c", start=30, end=50, score=10)
    assert [x.id for x in dedupe([a, b, c])] == ["b", "c"]


def test_select_highlights_end_to_end_with_fake_llm():
    tr = make_transcript(40)
    out = select_highlights(tr, CAMPAIGN, FakeLLM(clip_len=15), chunk_sec=1200, overlap_sec=60)
    assert len(out) == CAMPAIGN.clip_count
    assert [c.id for c in out] == ["c01", "c02"]
    assert out[0].start < out[1].start


def test_select_retries_on_invalid_json():
    tr = make_transcript(10)
    replies = iter(["not json at all", json.dumps({"highlights": [
        {"start_time": 1, "end_time": 15, "score": 70, "hook_sentence": "h"}]})])  # fmt: skip
    llm = FakeLLM(responder=lambda s, p: next(replies))
    out = select_highlights(tr, CAMPAIGN, llm, chunk_sec=1200, overlap_sec=60)
    assert len(out) == 1 and len(llm.calls) == 2
    assert "ONLY valid JSON" in llm.calls[1][1]


def test_select_non_retryable_llm_error_propagates():
    def boom(s, p):
        raise LLMError("auth", retryable=False)

    with pytest.raises(LLMError):
        select_highlights(make_transcript(5), CAMPAIGN, FakeLLM(responder=boom),
                          chunk_sec=1200, overlap_sec=60)  # fmt: skip
