from __future__ import annotations

import wave
from pathlib import Path

from clipfactory.backends.transcriber.base import build_transcript, make_word
from clipfactory.schemas import Transcript, Word

_SENTENCES = [
    "Никто не говорит об этом но секрет очень простой",
    "Я был полностью неправ насчёт денег и вот почему",
    "Сделай одну вещь утром и твой день изменится",
    "Это самая большая ошибка новичков в бизнесе",
    "Представь что у тебя есть всего десять минут",
    "Вот конкретный совет который работает каждый раз",
]


class FakeTranscriber:
    """Детерминированный транскрипт под длительность WAV — для тестов и dev."""

    name = "fake"
    model = "fake-1"

    def __init__(self, word_sec: float = 0.4, gap_sec: float = 0.1, pause_sec: float = 0.7):
        self.word_sec = word_sec
        self.gap_sec = gap_sec
        self.pause_sec = pause_sec

    def transcribe(self, audio_path: Path, language: str | None = None) -> Transcript:
        with wave.open(str(audio_path), "rb") as w:
            duration = w.getnframes() / float(w.getframerate())
        segments: list[tuple[float, float, str, list[Word]]] = []
        t, i = 0.2, 0
        while True:
            text = _SENTENCES[i % len(_SENTENCES)]
            words: list[Word] = []
            for token in text.split():
                if t + self.word_sec > duration:
                    break
                words.append(make_word(token, t, t + self.word_sec, 0.95))
                t += self.word_sec + self.gap_sec
            if not words:
                break
            segments.append((words[0].start, words[-1].end, " ".join(w.text for w in words), words))
            t += self.pause_sec
            i += 1
        return build_transcript(
            segments, language=language or "ru", model=self.model, duration=round(duration, 3)
        )
