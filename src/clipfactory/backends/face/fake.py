from __future__ import annotations

from collections.abc import Callable

import numpy as np

from clipfactory.backends.face.base import FaceBox


class FakeFaceDetector:
    """Скриптуемый детектор: ``script(t) -> list[FaceBox]``. По умолчанию лицо слева."""

    name = "fake"

    def __init__(self, script: Callable[[float], list[FaceBox]] | None = None) -> None:
        self.script = script or (lambda t: [FaceBox(0.2, 0.3, 0.15, 0.25, 0.9)])
        self.calls = 0

    def detect(self, frame: np.ndarray, t: float) -> list[FaceBox]:
        self.calls += 1
        return list(self.script(t))

    def close(self) -> None:
        pass
