from clipfactory.backends.face.base import FaceBox
from clipfactory.media.filters import crop_filter, piecewise_expr
from clipfactory.pipeline.reframe import Sample, crop_size, plan_reframe
from clipfactory.schemas import CropKeyframe


def face_at(cx: float, size: float = 0.15) -> FaceBox:
    return FaceBox(cx - size / 2, 0.3, size, size * 1.5, 0.9)


def samples(fn, duration: float = 10.0, fps: float = 4.0) -> list[Sample]:
    n = int(duration * fps)
    return [Sample(t=i / fps, faces=fn(i / fps)) for i in range(n)]


def test_crop_size_landscape_and_portrait():
    assert crop_size(1920, 1080) == (606, 1080)
    assert crop_size(1080, 1920) == (1080, 1920)
    assert crop_size(1080, 1350) == (758, 1350)  # 4:5 шире 9:16 — режем по ширине
    assert crop_size(720, 1600) == (720, 1280)  # уже 9:16 — режем по высоте


def test_no_faces_center_crop():
    plan = plan_reframe(samples(lambda t: []), [], 10, 1920, 1080)
    assert len(plan.keyframes) == 1
    k = plan.keyframes[0]
    assert abs((k.x + k.w / 2) - 960) <= 2


def test_jitter_does_not_move_camera():
    jitter = lambda t: [face_at(0.3 + (0.03 if int(t * 4) % 2 else -0.03))]  # noqa: E731
    plan = plan_reframe(samples(jitter), [], 10, 1920, 1080)
    assert len(plan.keyframes) == 1


def test_brief_glance_ignored_sustained_move_followed():
    def fn(t):
        if 3.0 <= t < 3.4:  # короткий скачок (< hold_sec) — игнор
            return [face_at(0.8)]
        if t >= 6.0:  # устойчивый переход
            return [face_at(0.75)]
        return [face_at(0.25)]

    plan = plan_reframe(samples(fn), [], 10, 1920, 1080)
    assert len(plan.keyframes) == 2
    assert plan.keyframes[0].x < plan.keyframes[1].x
    assert 5.9 <= plan.keyframes[1].t <= 6.1


def test_scene_cut_switches_immediately():
    fn = lambda t: [face_at(0.25 if t < 5 else 0.75)]  # noqa: E731
    plan = plan_reframe(samples(fn), [5.0], 10, 1920, 1080)
    assert [k.t for k in plan.keyframes] == [0.0, 5.0]


def test_override_and_bounds():
    plan = plan_reframe([], [], 10, 1920, 1080, center_override=1.0)
    k = plan.keyframes[0]
    assert k.x + k.w <= 1920 and k.x % 2 == 0


def test_piecewise_expression():
    kfs = [CropKeyframe(t=0, x=10, y=0, w=100, h=100), CropKeyframe(t=2.5, x=50, y=0, w=100, h=100)]
    assert piecewise_expr(kfs, "x") == "if(lt(t\\,2.500)\\,10\\,50)"
    assert crop_filter(kfs).startswith("crop=w=100:h=100:x=")


def test_mediapipe_detector_smoke():
    """Реальный MediaPipe, если есть модель и системные библиотеки; лиц в шуме нет."""
    import numpy as np
    import pytest

    from clipfactory.backends.face.mediapipe import FaceDetectorError, MediaPipeFaceDetector
    from tests.helpers import ROOT

    model = ROOT / "data" / "models" / "blaze_face_short_range.tflite"
    if not model.exists():
        pytest.skip("face model not downloaded (make face-model)")
    try:
        det = MediaPipeFaceDetector(model)
    except FaceDetectorError as e:
        pytest.skip(f"mediapipe unavailable here: {e}")
    frame = np.random.default_rng(0).integers(0, 255, (180, 320, 3), dtype=np.uint8)
    assert det.detect(frame, 0.0) == []
    det.close()
