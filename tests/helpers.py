from __future__ import annotations

from pathlib import Path

from clipfactory.backends.face.fake import FakeFaceDetector
from clipfactory.backends.llm.fake import FakeLLM
from clipfactory.backends.transcriber.fake import FakeTranscriber
from clipfactory.config import Settings
from clipfactory.services import App

ROOT = Path(__file__).resolve().parents[1]


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        data_dir=tmp_path / "data",
        campaigns_dir=ROOT / "config" / "campaigns",
        accounts_file=ROOT / "config" / "accounts.example.yaml",
        transcriber="fake",
        llm_provider="fake",
        face_detector="fake",
        encoder="x264",
        caption_font="DejaVu Sans",
        analysis_fps=2.0,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_app(tmp_path: Path, *, llm=None, face=None, transcriber=None, **overrides) -> App:
    llm = llm or FakeLLM(clip_len=25.0, per_chunk=3)
    face = face or FakeFaceDetector()
    transcriber = transcriber or FakeTranscriber()
    return App(
        make_settings(tmp_path, **overrides),
        transcriber_factory=lambda: transcriber,
        llm_factory=lambda: llm,
        face_factory=lambda: face,
    )
