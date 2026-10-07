"""reframe: кусочно-постоянный кроп 9:16 по лицам и сменам сцен.

Идея сегментного кропа (сегменты по сценам, слияние близких соседних сегментов)
адаптирована из ClipsAI resize (MIT, см. NOTICE) и переписана под MediaPipe:
камера стоит на месте, пока цель не ушла дальше порога дольше hold_sec.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from clipfactory.backends.face.base import FaceBox
from clipfactory.media import ffmpeg
from clipfactory.media.probe import probe
from clipfactory.pipeline import clipcache
from clipfactory.pipeline.context import StageContext, ValidationFailed
from clipfactory.schemas import (
    CropKeyframe,
    Highlights,
    Layout,
    ReframePlan,
    StageName,
    StageResult,
)

TARGET_W, TARGET_H = 1080, 1920
ANALYSIS_WIDTH = 320


@dataclass(frozen=True)
class ReframeParams:
    move_threshold: float = 0.08  # доля ширины исходника, после которой камера «хочет» сдвинуться
    hold_sec: float = 0.75  # столько цель должна стоять в новом месте
    merge_threshold: float = 0.04  # соседние сегменты ближе этого сливаются
    min_segment_sec: float = 1.0
    min_face_ratio: float = 0.2  # в сцене меньше лиц — center crop
    scene_threshold: float = 0.35


@dataclass(frozen=True)
class Sample:
    t: float
    faces: list[FaceBox]


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def crop_size(src_w: int, src_h: int) -> tuple[int, int]:
    """Максимальный кроп 9:16 внутри кадра."""
    if src_w / src_h > TARGET_W / TARGET_H:
        h = _even(src_h)
        return min(_even(h * TARGET_W / TARGET_H), _even(src_w)), h
    w = _even(src_w)
    return w, min(_even(w * TARGET_H / TARGET_W), _even(src_h))


def pick_face(faces: list[FaceBox], current_cx: float | None) -> FaceBox | None:
    """Самое заметное лицо; при близком размере — то, что ближе к текущему центру."""
    if not faces:
        return None
    best = max(faces, key=lambda f: f.area * f.score)
    if current_cx is None:
        return best
    close = [f for f in faces if f.area * f.score >= 0.6 * best.area * best.score]
    return min(close, key=lambda f: abs(f.cx - current_cx))


def plan_scene(samples: list[Sample], start: float, p: ReframeParams) -> list[tuple[float, float]]:
    """Сегменты (t_start, center_x 0..1) внутри одной сцены с гистерезисом."""
    with_face = [s for s in samples if s.faces]
    if not samples or len(with_face) < max(1, p.min_face_ratio * len(samples)):
        return [(start, 0.5)]

    # Начальная позиция: медиана первых ~hold_sec целей.
    first_t = with_face[0].t
    init = [pick_face(s.faces, None).cx for s in with_face if s.t <= first_t + p.hold_sec]
    current = statistics.median(init)
    segments = [(start, current)]
    pending: list[tuple[float, float]] = []
    for s in samples:
        face = pick_face(s.faces, current)
        if face is None:
            continue
        if abs(face.cx - current) > p.move_threshold:
            if (
                pending
                and abs(face.cx - statistics.median(c for _, c in pending)) > p.move_threshold
            ):
                pending = []  # цель скачет — начинаем отсчёт заново
            pending.append((s.t, face.cx))
            if pending[-1][0] - pending[0][0] >= p.hold_sec:
                current = statistics.median(c for _, c in pending)
                segments.append((pending[0][0], current))
                pending = []
        else:
            pending = []
    return segments


def merge_segments(segs: list[tuple[float, float]], p: ReframeParams) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for t, cx in segs:
        if merged and abs(cx - merged[-1][1]) < p.merge_threshold:
            continue
        if merged and t - merged[-1][0] < p.min_segment_sec and len(merged) > 1:
            merged[-1] = (merged[-1][0], cx)  # слишком короткий — переписываем позицию
            continue
        merged.append((t, cx))
    return merged


def plan_reframe(
    samples: list[Sample],
    scene_cuts: list[float],
    duration: float,
    src_w: int,
    src_h: int,
    params: ReframeParams = ReframeParams(),
    center_override: float | None = None,
) -> ReframePlan:
    cw, ch = crop_size(src_w, src_h)

    def kf(t: float, cx: float) -> CropKeyframe:
        x = int(round(cx * src_w - cw / 2))
        x = max(0, min(src_w - cw, x)) // 2 * 2
        y = max(0, (src_h - ch) // 2) // 2 * 2
        return CropKeyframe(t=round(t, 3), x=x, y=y, w=cw, h=ch)

    if center_override is not None:
        segs = [(0.0, min(1.0, max(0.0, center_override)))]
    else:
        bounds = [0.0] + [c for c in sorted(scene_cuts) if 0.3 < c < duration - 0.3] + [duration]
        segs = []
        for a, b in zip(bounds, bounds[1:], strict=False):
            scene_samples = [s for s in samples if a <= s.t < b]
            segs.extend(plan_scene(scene_samples, a, params))
        segs = merge_segments(segs, params)

    keyframes: list[CropKeyframe] = []
    for t, cx in segs:
        k = kf(t, cx)
        if keyframes and (k.x, k.y) == (keyframes[-1].x, keyframes[-1].y):
            continue
        keyframes.append(k)
    keyframes[0] = keyframes[0].model_copy(update={"t": 0.0})
    return ReframePlan(
        source_width=src_w,
        source_height=src_h,
        target_width=TARGET_W,
        target_height=TARGET_H,
        keyframes=keyframes,
    )


class ReframeStage:
    name = StageName.reframe
    version = 1

    def __init__(self, params: ReframeParams = ReframeParams()) -> None:
        self.params = params

    @staticmethod
    def needs_analysis(ctx: StageContext) -> bool:
        """fit_blur показывает кадр целиком: лица и смены сцен не нужны."""
        return ctx.campaign.layout is Layout.crop

    def config(self, ctx: StageContext) -> dict[str, Any]:
        cfg = {
            "face": ctx.backends.identity("face"),
            "analysis_fps": ctx.settings.analysis_fps,
            "params": self.params.__dict__,
            "overrides": ctx.overrides.crop_center_x,
        }
        if not self.needs_analysis(ctx):  # ключ только для не-дефолтной раскладки
            cfg = {"layout": ctx.campaign.layout.value}
        return cfg

    def input_keys(self, ctx: StageContext) -> list[str]:
        return [ctx.key("source.mp4"), ctx.key("highlights.json")]

    def run(self, ctx: StageContext) -> StageResult:
        src = ctx.local_path(ctx.key("source.mp4"))
        info = probe(src)
        src_w, src_h = info.display_size
        aw = ANALYSIS_WIDTH
        ah = _even(src_h * aw / src_w)
        highlights = ctx.read_model(ctx.key("highlights.json"), Highlights)
        source_sha = ctx.storage.checksum(ctx.key("source.mp4"))
        outputs = []
        segments_total = 0
        reused = 0
        for cand in highlights.candidates:
            override = ctx.overrides.crop_center_x.get(cand.id)
            key = ctx.clip_key(cand.id, "reframe.json")
            fp = clipcache.fingerprint(
                v=self.version, source=source_sha, start=cand.start, end=cand.end,
                face=ctx.backends.identity("face"), fps=ctx.settings.analysis_fps,
                params=self.params.__dict__, override=override,
                **({} if self.needs_analysis(ctx) else {"layout": ctx.campaign.layout.value}),
            )  # fmt: skip
            if clipcache.reusable(ctx, self.name.value, cand.id, fp) is not None:
                outputs.append(key)
                reused += 1
                continue
            samples: list[Sample] = []
            cuts: list[float] = []
            if not self.needs_analysis(ctx):
                override = 0.5
            if override is None:
                cuts = ffmpeg.detect_scenes(
                    src,
                    start=cand.start,
                    duration=cand.duration,
                    threshold=self.params.scene_threshold,
                )
                for t, frame in ffmpeg.iter_frames(
                    src, start=cand.start, duration=cand.duration,
                    fps=ctx.settings.analysis_fps, width=aw, height=ah,
                ):  # fmt: skip
                    if ctx.cancel.is_set():
                        raise ffmpeg.FFmpegCancelled("reframe cancelled")
                    samples.append(Sample(t=t, faces=ctx.backends.face.detect(frame, t)))
            plan = plan_reframe(
                samples, cuts, cand.duration, src_w, src_h, self.params, center_override=override
            )
            ctx.write_model(key, plan)
            clipcache.record(ctx, self.name.value, cand.id, fp, [key])
            outputs.append(key)
            segments_total += len(plan.keyframes)
        return StageResult(
            stage=self.name,
            outputs=outputs,
            info={"keyframes": segments_total, "clips_reused": reused},
        )

    def validate(self, ctx: StageContext, outputs: list[str]) -> None:
        highlights = ctx.read_model(ctx.key("highlights.json"), Highlights)
        for cand in highlights.candidates:
            plan = ctx.read_model(ctx.clip_key(cand.id, "reframe.json"), ReframePlan)
            if plan.target_width != TARGET_W or plan.target_height != TARGET_H:
                raise ValidationFailed(f"{cand.id}: unexpected target size")
