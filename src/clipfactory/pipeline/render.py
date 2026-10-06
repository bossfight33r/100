"""render: source + reframe.json + captions.ass -> final.mp4, thumb.jpg, meta.json.

Один вызов ffmpeg на клип: trim, кроп по времени, scale 1080x1920, ASS, loudnorm,
30 fps, AAC 128k, H.264, +faststart. Потом кадр-превью и метаданные под кампанию.
"""

from __future__ import annotations

import hashlib
import re
from string import Template
from typing import Any

from clipfactory.backends.llm.base import LLMError, parse_json_loose
from clipfactory.log import get_logger
from clipfactory.media import ffmpeg
from clipfactory.media.filters import render_filtergraph
from clipfactory.media.probe import ProbeError, probe
from clipfactory.pipeline import clipcache
from clipfactory.pipeline.context import StageContext, ValidationFailed, config_hash
from clipfactory.pipeline.select import load_prompt
from clipfactory.schemas import (
    Campaign,
    ClipCandidate,
    ClipMeta,
    Highlights,
    Platform,
    PlatformClipMeta,
    ReframePlan,
    StageName,
    StageResult,
)

log = get_logger(__name__)

TITLE_MAX = {Platform.youtube: 100, Platform.tiktok: 150, Platform.instagram: 150}
_TAG_RE = re.compile(r"[^\w]+", re.UNICODE)


# ---------------------------------------------------------------- metadata


def _contains_forbidden(text: str, forbidden: list[str]) -> bool:
    low = text.lower()
    return any(f.lower() in low for f in forbidden if f.strip())


def normalize_tag(tag: str) -> str | None:
    body = _TAG_RE.sub("", tag.lstrip("#"))
    return f"#{body}" if body else None


def enforce_campaign_rules(
    meta: PlatformClipMeta, campaign: Campaign, cand: ClipCandidate
) -> PlatformClipMeta:
    """must_include_tags, mentions и forbidden применяются детерминированно, без доверия LLM."""
    forbidden = campaign.forbidden
    title = meta.title.strip()
    if not title or _contains_forbidden(title, forbidden):
        title = cand.title or cand.hook or "Clip"
        if _contains_forbidden(title, forbidden):
            title = "Clip"
    title = title[: TITLE_MAX[meta.platform]]

    sentences = re.split(r"(?<=[.!?…])\s+", meta.description.strip())
    description = " ".join(s for s in sentences if s and not _contains_forbidden(s, forbidden))

    tags: list[str] = []
    for raw in [*campaign.must_include_tags, *meta.hashtags]:
        tag = normalize_tag(raw)
        if (
            tag
            and tag.lower() not in {t.lower() for t in tags}
            and not _contains_forbidden(tag, forbidden)
        ):
            tags.append(tag)
    mentions = [m for m in campaign.mentions if m not in description]
    if mentions:
        description = (description + "\n\n" + " ".join(mentions)).strip()
    return PlatformClipMeta(
        platform=meta.platform, title=title, description=description, hashtags=tags
    )


def generate_meta(
    llm: Any, campaign: Campaign, cand: ClipCandidate, clip_text: str
) -> list[PlatformClipMeta]:
    system = Template(load_prompt("metadata.md")).substitute(
        forbidden=", ".join(campaign.forbidden) or "(none)",
        notes=campaign.notes.strip() or "(none)",
    )
    result = []
    for platform in campaign.platforms:
        prompt = (
            f"PLATFORM: {platform.value}\n"
            f"Hook: {cand.hook}\nWhy it works: {cand.reason}\n"
            f"Clip transcript:\n{clip_text[:4000]}"
        )
        data: dict[str, Any] = {}
        for _ in range(2):
            try:
                parsed = parse_json_loose(
                    llm.complete(system=system, prompt=prompt, max_tokens=2000)
                )
            except LLMError as e:
                if not e.retryable:
                    raise
                continue
            except ValueError:
                continue
            if isinstance(parsed, dict):
                data = parsed
                break
        tags = data.get("hashtags") if isinstance(data.get("hashtags"), list) else []
        raw = PlatformClipMeta(
            platform=platform,
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            hashtags=[str(t) for t in tags],
        )
        result.append(enforce_campaign_rules(raw, campaign, cand))
    return result


# ---------------------------------------------------------------- render


def render_args(
    *,
    source: str,
    cand: ClipCandidate,
    plan: ReframePlan,
    encoder_args: list[str],
    fps: int,
    has_audio: bool,
    graph: str,
    output: str,
) -> list[str]:
    args = [
        "-ss", f"{cand.start:.3f}", "-i", source, "-t", f"{cand.duration:.3f}",
        "-filter_complex", graph,
        "-map", "[v]",
    ]  # fmt: skip
    if has_audio:
        args += ["-map", "[a]", "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    args += [
        *encoder_args, "-r", str(fps), "-fps_mode", "cfr",
        "-movflags", "+faststart", "-map_metadata", "-1", output,
    ]  # fmt: skip
    return args


def validate_video(path: Any, expected_duration: float, has_audio: bool, fps: int) -> None:
    try:
        info = probe(path)
    except ProbeError as e:
        raise ValidationFailed(f"rendered file is unreadable: {e}") from e
    v = info.video
    if v is None or (v.width, v.height) != (1080, 1920):
        raise ValidationFailed(f"expected 1080x1920, got {v and (v.width, v.height)}")
    if v.codec != "h264":
        raise ValidationFailed(f"expected h264, got {v.codec}")
    tolerance = 2.0 / fps + 0.1
    if abs(info.duration - expected_duration) > tolerance:
        raise ValidationFailed(f"duration {info.duration:.3f}s, expected {expected_duration:.3f}s")
    if has_audio and (info.audio is None or info.audio.codec != "aac"):
        raise ValidationFailed("expected an AAC audio track")


class RenderStage:
    name = StageName.render
    version = 1

    @staticmethod
    def meta_config(ctx: StageContext) -> dict[str, Any]:
        return {
            "llm": ctx.backends.identity("llm"),
            "meta_prompt_sha": hashlib.sha256(load_prompt("metadata.md").encode()).hexdigest(),
            "platforms": [p.value for p in ctx.campaign.platforms],
            "tags": ctx.campaign.must_include_tags,
            "mentions": ctx.campaign.mentions,
            "forbidden": ctx.campaign.forbidden,
            "notes": ctx.campaign.notes,
        }

    @staticmethod
    def reusable_meta(ctx: StageContext, key: str, meta_hash: str) -> list[PlatformClipMeta] | None:
        """Перерендер видео не должен перегенерировать метаданные (LLM недетерминирована)."""
        if not ctx.storage.exists(key):
            return None
        try:
            old = ctx.read_model(key, ClipMeta)
        except ValueError:
            return None
        return old.platforms if old.meta_hash == meta_hash else None

    def config(self, ctx: StageContext) -> dict[str, Any]:
        enc = ctx.backends.encoder
        return {
            "encoder": enc.video_args(fps=ctx.settings.output_fps),
            "fps": ctx.settings.output_fps,
            "fonts_dir": str(ctx.settings.caption_fonts_dir or ""),
            **self.meta_config(ctx),
        }

    def input_keys(self, ctx: StageContext) -> list[str]:
        keys = [ctx.key("source.mp4"), ctx.key("highlights.json"), ctx.key("transcript.json")]
        highlights = ctx.read_model(ctx.key("highlights.json"), Highlights)
        for c in highlights.candidates:
            keys += [ctx.clip_key(c.id, "reframe.json"), ctx.clip_key(c.id, "captions.ass")]
        return keys

    def run(self, ctx: StageContext) -> StageResult:
        from clipfactory.schemas import Transcript

        src = ctx.local_path(ctx.key("source.mp4"))
        has_audio = probe(src).audio is not None
        highlights = ctx.read_model(ctx.key("highlights.json"), Highlights)
        transcript = ctx.read_model(ctx.key("transcript.json"), Transcript)
        fps = ctx.settings.output_fps
        encoder = ctx.backends.encoder
        source_sha = ctx.storage.checksum(ctx.key("source.mp4"))
        outputs: list[str] = []
        reused = 0
        for cand in highlights.candidates:
            video_key = ctx.clip_key(cand.id, "final.mp4")
            thumb_key = ctx.clip_key(cand.id, "thumb.jpg")
            fp = clipcache.fingerprint(
                v=self.version, source=source_sha, start=cand.start, end=cand.end,
                reframe=ctx.storage.checksum(ctx.clip_key(cand.id, "reframe.json")),
                captions=ctx.storage.checksum(ctx.clip_key(cand.id, "captions.ass")),
                encoder=encoder.video_args(fps=fps), fps=fps,
                fonts_dir=str(ctx.settings.caption_fonts_dir or ""),
            )  # fmt: skip
            if clipcache.reusable(ctx, self.name.value, cand.id, fp) is not None:
                reused += 1
            else:
                self._render_clip(ctx, cand, src, has_audio, fps, encoder)
                clipcache.record(ctx, self.name.value, cand.id, fp, [video_key, thumb_key])
            meta_key = self._write_meta(ctx, cand, transcript)
            outputs += [video_key, thumb_key, meta_key]
        return StageResult(
            stage=self.name, outputs=outputs, info={"encoder": encoder.name, "clips_reused": reused}
        )

    def _render_clip(
        self,
        ctx: StageContext,
        cand: ClipCandidate,
        src: Any,
        has_audio: bool,
        fps: int,
        encoder: Any,
    ) -> None:
        plan = ctx.read_model(ctx.clip_key(cand.id, "reframe.json"), ReframePlan)
        ass_path = ctx.local_path(ctx.clip_key(cand.id, "captions.ass"))
        clip_dir = ass_path.parent
        graph = render_filtergraph(
            keyframes=plan.keyframes,
            target_width=plan.target_width,
            target_height=plan.target_height,
            fps=fps,
            ass_file=ass_path.name,  # относительный путь: cwd = каталог клипа
            fonts_dir=str(ctx.settings.caption_fonts_dir)
            if ctx.settings.caption_fonts_dir
            else None,
            has_audio=has_audio,
        )
        video_key = ctx.clip_key(cand.id, "final.mp4")
        tmp_video = clip_dir / ".final.tmp.mp4"
        log_path = ctx.local_path(ctx.log_key(self.name, f"-{cand.id}"))
        ffmpeg.ffmpeg(
            render_args(
                source=str(src), cand=cand, plan=plan,
                encoder_args=encoder.video_args(fps=fps), fps=fps, has_audio=has_audio,
                graph=graph, output=tmp_video.name,
            ),
            cwd=clip_dir, log_path=log_path, cancel=ctx.cancel,
            timeout=ctx.settings.ffmpeg_timeout_sec,
        )  # fmt: skip
        validate_video(tmp_video, cand.duration, has_audio, fps)

        # превью — из только что отрендеренного файла: для удалённого хранилища
        # local_path(video_key) — копия в scratch, сделанная до загрузки нового видео
        thumb_key = ctx.clip_key(cand.id, "thumb.jpg")
        thumb_path = ctx.local_path(thumb_key)
        ffmpeg.ffmpeg(
            ["-ss", f"{min(1.0, cand.duration / 3):.3f}", "-i", str(tmp_video),
             "-frames:v", "1", "-q:v", "3", str(thumb_path)],
            log_path=log_path.with_name(log_path.stem + "-thumb.log"), cancel=ctx.cancel,
        )  # fmt: skip
        ctx.storage.put_file(video_key, tmp_video)
        tmp_video.unlink(missing_ok=True)
        ctx.commit(thumb_key, thumb_path)
        log.info("render.clip_done", job_id=ctx.job.id, clip_id=cand.id, duration=cand.duration)

    def _write_meta(self, ctx: StageContext, cand: ClipCandidate, transcript: Any) -> str:
        clip_text = " ".join(w.text for w in transcript.words if cand.start <= w.start < cand.end)
        meta_key = ctx.clip_key(cand.id, "meta.json")
        meta_hash = config_hash(
            {"candidate": cand.model_dump(mode="json"), "cfg": self.meta_config(ctx)}
        )
        metas = self.reusable_meta(ctx, meta_key, meta_hash)
        if metas is None:
            metas = generate_meta(ctx.backends.llm, ctx.campaign, cand, clip_text)
        ctx.write_model(
            meta_key,
            ClipMeta(
                clip_id=cand.id,
                candidate=cand,
                platforms=metas,
                duration=cand.duration,
                meta_hash=meta_hash,
            ),  # fmt: skip
        )
        return meta_key

    def validate(self, ctx: StageContext, outputs: list[str]) -> None:
        src = ctx.local_path(ctx.key("source.mp4"))
        has_audio = probe(src).audio is not None
        highlights = ctx.read_model(ctx.key("highlights.json"), Highlights)
        for cand in highlights.candidates:
            for name in ("final.mp4", "thumb.jpg", "meta.json"):
                if not ctx.storage.exists(ctx.clip_key(cand.id, name)):
                    raise ValidationFailed(f"{cand.id}/{name} is missing")
            validate_video(
                ctx.local_path(ctx.clip_key(cand.id, "final.mp4")),
                cand.duration,
                has_audio,
                ctx.settings.output_fps,
            )
            ctx.read_model(ctx.clip_key(cand.id, "meta.json"), ClipMeta)
