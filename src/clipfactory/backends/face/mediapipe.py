from __future__ import annotations

from pathlib import Path

import numpy as np

from clipfactory.backends.face.base import FaceBox


class FaceDetectorError(Exception):
    pass


class MediaPipeFaceDetector:
    """MediaPipe Tasks FaceDetector (BlazeFace short range), CPU, без torch."""

    name = "mediapipe"

    def __init__(self, model_path: Path, min_confidence: float = 0.5) -> None:
        model_path = Path(model_path)
        if not model_path.is_file():
            raise FaceDetectorError(
                f"MediaPipe face model not found at {model_path}; run `make face-model`"
            )
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions, vision

            options = vision.FaceDetectorOptions(
                base_options=BaseOptions(model_asset_path=str(model_path)),
                running_mode=vision.RunningMode.IMAGE,
                min_detection_confidence=min_confidence,
            )
            self._detector = vision.FaceDetector.create_from_options(options)
        except (ImportError, OSError, RuntimeError) as e:
            # на Linux без libEGL MediaPipe падает с OSError — это не временная ошибка
            raise FaceDetectorError(f"cannot initialize MediaPipe face detector: {e}") from e
        self._mp = mp

    def detect(self, frame: np.ndarray, t: float) -> list[FaceBox]:
        h, w = frame.shape[:2]
        image = self._mp.Image(
            image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(frame)
        )
        result = self._detector.detect(image)
        boxes = []
        for d in result.detections:
            bb = d.bounding_box
            score = d.categories[0].score if d.categories else 0.0
            x0 = max(0.0, bb.origin_x / w)
            y0 = max(0.0, bb.origin_y / h)
            bw = min(1.0 - x0, bb.width / w)
            bh = min(1.0 - y0, bb.height / h)
            if bw > 0 and bh > 0:
                boxes.append(FaceBox(x0, y0, bw, bh, float(score)))
        return boxes

    def close(self) -> None:
        self._detector.close()
