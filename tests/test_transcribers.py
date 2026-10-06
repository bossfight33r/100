"""Адаптеры транскриберов на настоящих типах библиотек (без скачивания весов)."""

from __future__ import annotations

import sys
import types

import pytest

from clipfactory.backends.transcriber.base import TranscriberError
from clipfactory.backends.transcriber.faster_whisper import FasterWhisperTranscriber
from clipfactory.backends.transcriber.mlx import MLXWhisperTranscriber

fw = pytest.importorskip("faster_whisper.transcribe")


def fw_segment(i, start, end, text, words):
    return fw.Segment(
        id=i, seek=0, start=start, end=end, text=text, tokens=[], avg_logprob=-0.2,
        compression_ratio=1.0, no_speech_prob=0.01, words=words, temperature=0.0,
    )  # fmt: skip


class FakeWhisperModel:
    def __init__(self, segments):
        self.segments = segments
        self.kwargs = None

    def transcribe(self, audio, **kwargs):
        self.kwargs = kwargs
        info = types.SimpleNamespace(language="ru", duration=4.0)
        return iter(self.segments), info


def make(model_segments) -> tuple[FasterWhisperTranscriber, FakeWhisperModel]:
    t = FasterWhisperTranscriber("tiny")
    t._model = FakeWhisperModel(model_segments)
    return t, t._model


def test_faster_whisper_adapter_maps_real_types(tmp_path):
    words = [
        fw.Word(start=0.0, end=0.42, word=" Привет,", probability=0.98),
        fw.Word(start=0.5, end=0.9, word=" мир", probability=0.91),
        fw.Word(start=0.95, end=0.95, word=" ", probability=0.1),  # пустое слово отбрасывается
    ]
    t, model = make([fw_segment(0, 0.0, 0.9, " Привет, мир", words)])
    tr = t.transcribe(tmp_path / "a.wav", language="ru")
    assert model.kwargs["word_timestamps"] is True  # пословные таймстемпы обязательны
    assert [w.text for w in tr.words] == ["Привет,", "мир"]
    assert tr.words[0].probability == pytest.approx(0.98)
    assert tr.language == "ru" and tr.duration == 4.0
    assert tr.model == "faster-whisper/tiny"
    assert tr.segments[0].text == "Привет, мир"


def test_faster_whisper_segment_without_words_is_rejected(tmp_path):
    t, _ = make([fw_segment(0, 0.0, 1.0, " текст", None)])
    with pytest.raises(TranscriberError, match="word timestamps"):
        t.transcribe(tmp_path / "a.wav")


def test_mlx_adapter_maps_dict_output(tmp_path, monkeypatch):
    import wave

    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 32000)
    calls = {}

    def transcribe(path, **kwargs):
        calls.update(kwargs)
        return {
            "language": "ru",
            "segments": [
                {"start": 0.0, "end": 1.0, "text": " Да нет",
                 "words": [{"word": " Да", "start": 0.0, "end": 0.3, "probability": 0.9},
                           {"word": " нет", "start": 0.4, "end": 1.0, "probability": 0.8}]},
            ],
        }  # fmt: skip

    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=transcribe))
    tr = MLXWhisperTranscriber("large-v3-turbo").transcribe(wav)
    assert calls["word_timestamps"] is True
    assert calls["path_or_hf_repo"] == "mlx-community/whisper-large-v3-turbo"
    assert [w.text for w in tr.words] == ["Да", "нет"] and tr.duration == 2.0
