from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class FaceBox:
    """Лицо в нормированных координатах кадра (0..1)."""

    x: float
    y: float
    w: float
    h: float
    score: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return self.w * self.h


@runtime_checkable
class FaceDetector(Protocol):
    name: str

    def detect(self, frame: np.ndarray, t: float) -> list[FaceBox]:
        """frame — RGB uint8 HxWx3; t — секунды от начала клипа."""
        ...

    def close(self) -> None: ...
