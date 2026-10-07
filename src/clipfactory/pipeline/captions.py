"""captions: transcript + highlights -> clips/{id}/captions.ass.

2–4 слова на экране, активное слово подсвечено, обводка, safe zone выше нижних 20%.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from clipfactory.pipeline.context import StageContext, ValidationFailed
from clipfactory.schemas import ClipCandidate, Highlights, StageName, StageResult, Transcript, Word

PLAY_W, PLAY_H = 1080, 1920
GROUP_BREAK_GAP = 0.5
MAX_GROUP_CHARS = 22
BREAK_PUNCT = (".", "!", "?", "…", ",", ":", ";")


@dataclass(frozen=True)
class CaptionStyle:
    font: str = "Arial"
    size: int = 84
    max_words: int = 3
    primary: str = "&H00FFFFFF"  # белый, формат &HAABBGGRR
    highlight: str = "&H0000E5FF"  # жёлтый
    outline_color: str = "&H00000000"
    outline: int = 6
    margin_v: int = int(PLAY_H * 0.26)  # нижний край текста выше нижних ~20% кадра


def escape_ass(text: str) -> str:
    """Нейтрализовать управляющие символы ASS: override-блоки и \\N/\\h-последовательности."""
    text = text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    return " ".join(text.split())


TITLE_MAX_CHARS = 60
_HASHTAG = re.compile(r"#\w+", re.UNICODE)


def overlay_text(title: str) -> str:
    """Текст заголовка поверх видео: без хэштегов и эмодзи (libass не рисует цветные)."""
    text = _HASHTAG.sub("", title)
    text = "".join(ch for ch in text if ord(ch) <= 0xFFFF and unicodedata.category(ch) != "So")
    text = " ".join(text.split())
    if len(text) > TITLE_MAX_CHARS:
        text = text[: TITLE_MAX_CHARS - 1].rsplit(" ", 1)[0].rstrip(",.:;—-") + "…"
    return text


def title_event(text: str, duration: float, y: int, size: int = 78) -> str:
    """Dialogue ASS: заголовок по центру по горизонтали, на высоте y, весь клип."""
    tags = f"{{\\an5\\pos({PLAY_W // 2},{y})\\fs{size}\\b1\\bord7\\shad0}}"
    return (
        f"Dialogue: 1,{ass_time(0)},{ass_time(duration)},Default,,0,0,0,,{tags}{escape_ass(text)}"
    )


def ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def clip_words(words: list[Word], cand: ClipCandidate) -> list[Word]:
    """Слова клипа во времени клипа (0 = начало клипа)."""
    out = []
    for w in words:
        if w.end <= cand.start + 0.02 or w.start >= cand.end - 0.02:
            continue
        s = max(0.0, w.start - cand.start)
        e = min(cand.duration, w.end - cand.start)
        text = w.text.strip()
        if text and e > s:
            out.append(
                Word(text=text, start=round(s, 3), end=round(e, 3), probability=w.probability)
            )
    return out


def group_words(words: list[Word], max_words: int) -> list[list[Word]]:
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            gap = w.start - cur[-1].end
            chars = sum(len(x.text) + 1 for x in cur) + len(w.text)
            if (
                len(cur) >= max_words
                or gap > GROUP_BREAK_GAP
                or cur[-1].text.endswith(BREAK_PUNCT)
                or chars > MAX_GROUP_CHARS
            ):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def build_ass(words: list[Word], duration: float, style: CaptionStyle) -> str:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_W}
PlayResY: {PLAY_H}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style.font},{style.size},{style.primary},{style.highlight},{style.outline_color},&H64000000,-1,0,0,0,100,100,0,0,1,{style.outline},0,2,80,80,{style.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    groups = group_words(words, style.max_words)
    for gi, group in enumerate(groups):
        next_start = groups[gi + 1][0].start if gi + 1 < len(groups) else duration
        group_end = min(next_start, group[-1].end + 0.3, duration)
        texts = [escape_ass(w.text) for w in group]
        for wi, w in enumerate(group):
            start = group[0].start if wi == 0 else w.start
            end = group[wi + 1].start if wi + 1 < len(group) else group_end
            if end - start < 0.01:
                continue
            parts = [
                f"{{\\c{style.highlight}&}}{t}{{\\c{style.primary}&}}" if k == wi else t
                for k, t in enumerate(texts)
            ]
            lines.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{' '.join(parts)}"
            )
    return header + "\n".join(lines) + "\n"


class CaptionsStage:
    name = StageName.captions
    version = 1

    def style(self, ctx: StageContext) -> CaptionStyle:
        s = ctx.settings
        return CaptionStyle(
            font=s.caption_font, size=s.caption_font_size, max_words=s.caption_max_words
        )

    def config(self, ctx: StageContext) -> dict[str, Any]:
        return {"style": self.style(ctx).__dict__, "nonce": ctx.overrides.caption_nonce}

    def input_keys(self, ctx: StageContext) -> list[str]:
        return [ctx.key("transcript.json"), ctx.key("highlights.json")]

    def run(self, ctx: StageContext) -> StageResult:
        transcript = ctx.read_model(ctx.key("transcript.json"), Transcript)
        highlights = ctx.read_model(ctx.key("highlights.json"), Highlights)
        style = self.style(ctx)
        outputs = []
        for cand in highlights.candidates:
            words = clip_words(transcript.words, cand)
            key = ctx.clip_key(cand.id, "captions.ass")
            ctx.storage.put_bytes(key, build_ass(words, cand.duration, style).encode("utf-8"))
            outputs.append(key)
        return StageResult(stage=self.name, outputs=outputs)

    def validate(self, ctx: StageContext, outputs: list[str]) -> None:
        for key in outputs:
            with ctx.storage.open_read(key) as f:
                head = f.read(64)
            if not head.startswith(b"[Script Info]"):
                raise ValidationFailed(f"{key} is not an ASS file")
