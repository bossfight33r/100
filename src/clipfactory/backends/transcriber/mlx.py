from __future__ import annotations

import importlib.util
import platform
import wave
from pathlib import Path

from clipfactory.backends.transcriber.base import TranscriberError, build_transcript, make_word
from clipfactory.schemas import Transcript

_MLX_REPOS = {
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
}


def mlx_available() -> bool:
    return (
        platform.system() == "Darwin"
        and platform.machine() == "arm64"
        and importlib.util.find_spec("mlx_whisper") is not None
    )


class MLXWhisperTranscriber:
    """mlx-whisper на Apple Silicon (Metal)."""

    name = "mlx"

    def __init__(self, model: str = "large-v3-turbo") -> None:
        self.model = model
        self.repo = _MLX_REPOS.get(model, model)

    def transcribe(
        self, audio_path: Path, language: str | None = None
    ) -> Transcript:  # pragma: no cover
        try:
            import mlx_whisper
        except ImportError as e:
            raise TranscriberError("mlx-whisper is not installed (uv pip install -e .[mac])") from e
        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=self.repo,
            language=language,
            word_timestamps=True,
            condition_on_previous_text=False,
        )
        with wave.open(str(audio_path), "rb") as w:
            duration = w.getnframes() / float(w.getframerate())
        raw = []
        for s in result.get("segments", []):
            words = [
                make_word(w["word"], w["start"], w["end"], w.get("probability"))
                for w in s.get("words", [])
            ]
            words = [w for w in words if w.text]
            raw.append((float(s["start"]), float(s["end"]), s.get("text", ""), words))
        return build_transcript(
            raw,
            language=language or result.get("language") or "unknown",
            model=f"mlx-whisper/{self.model}",
            duration=duration,
        )
