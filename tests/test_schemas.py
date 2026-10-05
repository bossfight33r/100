import pytest
from pydantic import ValidationError

from clipfactory.schemas import (
    Account,
    Campaign,
    ClipCandidate,
    CropKeyframe,
    ReframePlan,
    Transcript,
    Word,
)


def test_word_end_before_start_rejected():
    with pytest.raises(ValidationError):
        Word(text="x", start=2.0, end=1.0)


def test_transcript_roundtrip():
    w = Word(text="привет", start=0.0, end=0.5, probability=0.9)
    t = Transcript(language="ru", model="m", duration=1.0, words=[w], segments=[])
    assert Transcript.model_validate_json(t.model_dump_json()) == t


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        Word(text="x", start=0, end=1, foo=1)


def test_campaign_min_max():
    with pytest.raises(ValidationError):
        Campaign(id="c", name="c", rate_per_1k_views=1, platforms=["youtube"],
                 clip_min_sec=60, clip_max_sec=30)  # fmt: skip


def test_campaign_unknown_platform():
    with pytest.raises(ValidationError):
        Campaign(id="c", name="c", rate_per_1k_views=1, platforms=["myspace"])


@pytest.mark.parametrize("window", ["9:00-12:00", "12:00-09:00", "25:00-26:00", "10:00"])
def test_account_bad_windows(window):
    with pytest.raises(ValidationError):
        Account(id="a", platform="youtube", name="a", posting_windows=[window])


def test_account_bad_timezone():
    with pytest.raises(ValidationError):
        Account(id="a", platform="youtube", name="a", timezone="Mars/Olympus")


@pytest.mark.parametrize("ref", ["../etc/passwd", "ya29.secret token", "a/b"])
def test_account_token_ref_must_be_identifier(ref):
    with pytest.raises(ValidationError):
        Account(id="a", platform="youtube", name="a", token_ref=ref)


def test_clip_candidate_order():
    with pytest.raises(ValidationError):
        ClipCandidate(id="c", start=5, end=5, score=10)
    with pytest.raises(ValidationError):
        ClipCandidate(id="c", start=1, end=5, score=101)


def test_reframe_plan_bounds():
    kf = CropKeyframe(t=0, x=500, y=0, w=203, h=360)
    with pytest.raises(ValidationError):
        ReframePlan(source_width=640, source_height=360, keyframes=[kf])
    with pytest.raises(ValidationError):
        ReframePlan(source_width=640, source_height=360,
                    keyframes=[CropKeyframe(t=1, x=0, y=0, w=202, h=360)])  # fmt: skip
    ok = ReframePlan(source_width=640, source_height=360,
                     keyframes=[CropKeyframe(t=0, x=0, y=0, w=202, h=360)])  # fmt: skip
    assert ok.target_width == 1080 and ok.target_height == 1920
