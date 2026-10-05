import re

from clipfactory.pipeline.captions import (
    CaptionStyle,
    ass_time,
    build_ass,
    clip_words,
    escape_ass,
    group_words,
)
from clipfactory.schemas import ClipCandidate, Word


def w(text, s, e):
    return Word(text=text, start=s, end=e)


def test_escape_ass_neutralizes_overrides():
    out = escape_ass("{\\b1}жирный\\Nперенос\n}")
    assert "{" not in out and "}" not in out and "\\" not in out


def test_ass_time():
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(3725.456) == "1:02:05.46"


def test_grouping_limits():
    words = [w(f"слово{i}", i * 0.4, i * 0.4 + 0.3) for i in range(7)]
    groups = group_words(words, max_words=3)
    assert all(2 <= len(g) <= 3 for g in groups[:-1])
    # пауза и пунктуация разрывают группу
    words = [w("Привет.", 0, 0.3), w("как", 0.4, 0.6), w("дела", 1.5, 1.8)]
    assert [len(g) for g in group_words(words, 3)] == [1, 1, 1]


def test_clip_words_shifted_and_trimmed():
    cand = ClipCandidate(id="c", start=10, end=20, score=1)
    words = [w("до", 9, 9.5), w("раз", 10.1, 10.4), w("край", 19.9, 20.4), w("после", 21, 22)]
    out = clip_words(words, cand)
    assert [x.text for x in out] == ["раз", "край"]
    assert out[0].start == 0.1 and out[1].end == 10.0


def test_build_ass_highlight_and_safe_zone():
    style = CaptionStyle(font="DejaVu Sans")
    words = [w("один", 0.0, 0.4), w("два", 0.5, 0.9), w("три", 1.0, 1.4)]
    ass = build_ass(words, 2.0, style)
    assert "PlayResY: 1920" in ass
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(events) == 3
    # в каждом событии подсвечено ровно одно слово
    for e in events:
        assert e.count(style.highlight) == 1
    margin_v = int(re.search(r"Style: Default,.*,(\d+),1$", ass, re.M).group(1))
    assert margin_v >= 0.2 * 1920
    # события непрерывны: конец одного = начало следующего
    times = [e.split(",")[1:3] for e in events]
    assert times[0][1] == times[1][0] and times[1][1] == times[2][0]
