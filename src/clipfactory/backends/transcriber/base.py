from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

from clipfactory.schemas import Segment, Transcript, Word


class TranscriberError(Exception):
    pass


@runtime_checkable
class Transcriber(Protocol):
    """Распознавание речи с обязательными пословными таймстемпами."""

    name: str
    model: str

    def transcribe(self, audio_path: Path, language: str | None = None) -> Transcript: ...


def build_transcript(
    raw_segments: Iterable[tuple[float, float, str, list[Word]]],
    *,
    language: str,
    model: str,
    duration: float,
) -> Transcript:
    """Собрать Transcript и проверить, что у каждого сегмента с текстом есть слова."""
    segments: list[Segment] = []
    words: list[Word] = []
    for start, end, text, seg_words in raw_segments:
        text = text.strip()
        if not text:
            continue
        if not seg_words:
            raise TranscriberError(
                "backend returned a segment without word timestamps; word-level timing is required"
            )
        segments.append(Segment(start=start, end=max(end, start), text=text, words=seg_words))
        words.extend(seg_words)
    return Transcript(
        language=language, model=model, duration=duration, words=words, segments=segments
    )


def make_word(text: str, start: float, end: float, probability: float | None) -> Word:
    start = max(0.0, float(start))
    end = max(start, float(end))
    p = None if probability is None else min(1.0, max(0.0, float(probability)))
    return Word(text=text.strip(), start=round(start, 3), end=round(end, 3), probability=p)
