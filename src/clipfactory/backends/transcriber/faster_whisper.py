from __future__ import annotations

from pathlib import Path

from clipfactory.backends.transcriber.base import TranscriberError, build_transcript, make_word
from clipfactory.schemas import Transcript


class FasterWhisperTranscriber:
    """faster-whisper на CPU int8 (fallback для не-Apple Silicon и серверов без GPU)."""

    name = "faster_whisper"

    def __init__(self, model: str = "large-v3-turbo", cpu_threads: int = 0) -> None:
        self.model = model
        self.cpu_threads = cpu_threads
        self._model = None

    def _load(self):  # pragma: no cover - требует скачивания модели
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise TranscriberError("faster-whisper is not installed") from e
            self._model = WhisperModel(
                self.model, device="cpu", compute_type="int8", cpu_threads=self.cpu_threads
            )
        return self._model

    def transcribe(
        self, audio_path: Path, language: str | None = None
    ) -> Transcript:  # pragma: no cover
        model = self._load()
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=language,
            word_timestamps=True,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=False,
        )
        raw = []
        for s in segments_iter:
            words = [make_word(w.word, w.start, w.end, w.probability) for w in (s.words or [])]
            words = [w for w in words if w.text]
            raw.append((float(s.start), float(s.end), s.text or "", words))
        return build_transcript(
            raw,
            language=language or info.language,
            model=f"faster-whisper/{self.model}",
            duration=float(info.duration),
        )
