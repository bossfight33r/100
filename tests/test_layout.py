"""Раскладка fit_blur: весь кадр по центру, размытый фон сверху и снизу."""

from __future__ import annotations

import pytest

from clipfactory.media import ffmpeg
from clipfactory.media.filters import fit_dims, render_filtergraph
from clipfactory.media.probe import probe
from clipfactory.pipeline.reframe import ReframeStage
from clipfactory.pipeline.render import RenderStage
from clipfactory.schemas import Campaign, CropKeyframe, Layout
from tests.conftest import needs_ffmpeg
from tests.helpers import make_app


def test_fit_dims_landscape():
    assert fit_dims(1920, 1080, 1080, 1920) == (1080, 606, 1080, 606)
    # zoom 1.5: кадр шире экрана, края срезаются, высота растёт
    assert fit_dims(1920, 1080, 1080, 1920, 1.5) == (1620, 910, 1080, 910)


def test_fit_dims_vertical_source_fills_screen():
    assert fit_dims(1080, 1920, 1080, 1920) == (1080, 1920, 1080, 1920)


def test_campaign_layout_validation():
    base = dict(id="c", name="c", rate_per_1k_views=1, platforms=["youtube"])
    assert Campaign.model_validate(base).layout is Layout.crop
    with pytest.raises(ValueError):
        Campaign.model_validate({**base, "layout": "fit_blur", "fit_zoom": 3})


def test_filtergraph_fit_ignores_keyframes():
    kf = [CropKeyframe(t=0, x=0, y=0, w=202, h=360)]
    common = dict(
        keyframes=kf, target_width=1080, target_height=1920, fps=30,
        ass_file="captions.ass", fonts_dir=None, has_audio=True,
    )  # fmt: skip
    crop = render_filtergraph(**common)
    fit = render_filtergraph(**common, fit=(640, 360, 1.0))
    assert "crop=w=202" in crop and "boxblur" not in crop
    assert "boxblur" in fit and "crop=w=202" not in fit
    assert "overlay=(1080-1080)/2:(1920-606)/2" in fit
    assert fit.count("ass=filename=captions.ass") == 1 and fit.endswith("[a]")


@needs_ffmpeg
def test_fit_blur_render_keeps_whole_frame(tmp_path):
    """Красный кадр 16:9: центр — исходный цвет, сверху — затемнённый размытый фон."""
    src = tmp_path / "red.mp4"
    ffmpeg.ffmpeg(
        ["-f", "lavfi", "-i", "color=c=red:size=640x360:rate=25:duration=2",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(src)],
        timeout=60,
    )  # fmt: skip
    graph = render_filtergraph(
        keyframes=[CropKeyframe(t=0, x=0, y=0, w=202, h=360)],
        target_width=1080, target_height=1920, fps=25, ass_file=None, fonts_dir=None,
        has_audio=False, fit=(640, 360, 1.0),
    )  # fmt: skip
    out = tmp_path / "out.mp4"
    ffmpeg.ffmpeg(
        ["-i", str(src), "-filter_complex", graph, "-map", "[v]",
         "-c:v", "libx264", "-preset", "ultrafast", str(out)],
        timeout=120,
    )  # fmt: skip
    info = probe(out)
    assert info.display_size == (1080, 1920)
    (_, frame), *_ = ffmpeg.iter_frames(out, start=0.5, duration=0.1, fps=5, width=108, height=192)
    center, top = frame[96, 54], frame[10, 54]
    assert center[0] > 200 and center[1] < 60
    assert top[0] + 15 < center[0]  # фон темнее кадра


FIT_CAMPAIGN = """
id: gameplay
name: Gameplay
rate_per_1k_views: 1
platforms: [youtube]
clip_min_sec: 8
clip_max_sec: 12
clip_count: 1
layout: fit_blur
fit_zoom: 1.2
"""


class NoFaces:
    def detect(self, *a, **kw):  # pragma: no cover - не должен вызываться
        raise AssertionError("fit_blur must not run face detection")


@needs_ffmpeg
@pytest.mark.slow
def test_fit_blur_job_skips_face_analysis(tmp_path, long_synthetic_video):
    cdir = tmp_path / "campaigns"
    cdir.mkdir()
    (cdir / "gameplay.yaml").write_text(FIT_CAMPAIGN, encoding="utf-8")
    app = make_app(tmp_path, face=NoFaces(), campaigns_dir=cdir)
    job = app.create_job(str(long_synthetic_video), "gameplay")
    final = app.run_job(job.id)
    assert final.status.value == "awaiting_review", final.error_message
    ctx = app.context(final)
    assert probe(ctx.local_path(ctx.clip_key("c01", "final.mp4"))).display_size == (1080, 1920)
    assert ReframeStage().config(ctx) == {"layout": "fit_blur"}
    assert RenderStage.layout_config(ctx) == {"layout": "fit_blur", "fit_zoom": 1.2}


def test_crop_layout_cache_keys_unchanged(tmp_path, synthetic_video):
    app = make_app(tmp_path)
    ctx = app.context(app.create_job(str(synthetic_video), "example"))
    assert set(ReframeStage().config(ctx)) == {"face", "analysis_fps", "params", "overrides"}
    assert RenderStage.layout_config(ctx) == {}


def test_overlay_text_cleanup():
    from clipfactory.pipeline.captions import overlay_text, title_event

    assert overlay_text("Клатч 1v4 🔥 #cs2 #shorts") == "Клатч 1v4"
    long = overlay_text("Очень длинный заголовок " * 6)
    assert len(long) <= 60 and long.endswith("…") and "  " not in long
    ev = title_event("{\\pos(0,0)} взлом", 10, 437)
    assert ev.count("{") == 1 and "\\pos(540,437)" in ev and "0:00:10.00" in ev


def test_title_y_inside_blurred_band(tmp_path, synthetic_video):
    from clipfactory.schemas import PlatformClipMeta, ReframePlan

    cdir = tmp_path / "campaigns"
    cdir.mkdir()
    (cdir / "gameplay.yaml").write_text(FIT_CAMPAIGN + "title_overlay: true\n", encoding="utf-8")
    app = make_app(tmp_path, campaigns_dir=cdir)
    ctx = app.context(app.create_job(str(synthetic_video), "gameplay"))
    plan = ReframePlan(
        source_width=1920, source_height=1080,
        keyframes=[CropKeyframe(t=0, x=0, y=0, w=606, h=1080)],
    )  # fmt: skip
    y = RenderStage.title_y(ctx, plan)
    band = (1920 - fit_dims(1920, 1080, 1080, 1920, 1.2)[3]) // 2
    assert 200 < y < band - 60
    metas = [
        PlatformClipMeta(platform="tiktok", title="tt", description=""),
        PlatformClipMeta(platform="youtube", title="Эйс #cs2", description=""),
    ]
    assert RenderStage.overlay_title(ctx, metas) == "Эйс"
    off = make_app(tmp_path / "off")
    ctx_off = off.context(off.create_job(str(synthetic_video), "example"))
    assert RenderStage.overlay_title(ctx_off, metas) is None
