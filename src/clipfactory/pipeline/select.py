"""select: transcript.json -> highlights.json.

Кампания `selection: signals` выбирает без LLM по сигналам (pipeline/signals.py, ADR-0014).

Чанки ~20 мин с overlap ~60 сек -> LLM -> нормализация -> дедуп по overlap ->
подгонка границ по словам и паузам -> clip_min/max -> top-N.
Идеи промпта, чанкинга и дедупа — из AI-Youtube-Shorts-Generator (MIT, см. NOTICE).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from importlib import resources
from string import Template
from typing import Any

from clipfactory.backends.llm.base import LLMError, parse_json_loose
from clipfactory.log import get_logger
from clipfactory.pipeline.context import NoHighlightsError, StageContext, ValidationFailed
from clipfactory.schemas import (
    Campaign,
    ChatActivity,
    ClipCandidate,
    Highlights,
    SelectionMode,
    SourceInfo,
    StageName,
    StageResult,
    Transcript,
    Word,
)

log = get_logger(__name__)

MAX_LLM_ATTEMPTS = 3
SNAP_WINDOW = 2.0  # сек вокруг границы от LLM, где ищем слово
PAUSE_SEC = 0.3  # пауза, считающаяся естественной границей
SENTENCE_END = (".", "!", "?", "…")
DEDUPE_OVERLAP = 0.5


def load_prompt(name: str) -> str:
    return resources.files("clipfactory.prompts").joinpath(name).read_text(encoding="utf-8")


@dataclass(frozen=True)
class RawHighlight:
    start: float
    end: float
    score: int
    title: str
    hook: str
    reason: str


# ---------------------------------------------------------------- chunking


@dataclass(frozen=True)
class Chunk:
    start: float
    end: float
    text: str


def build_chunks(transcript: Transcript, chunk_sec: float, overlap_sec: float) -> list[Chunk]:
    """Чанки с перекрытием. Таймстемпы в тексте абсолютные (от начала видео)."""
    duration = transcript.duration or (transcript.segments[-1].end if transcript.segments else 0)
    step = max(chunk_sec - overlap_sec, 1.0)
    chunks: list[Chunk] = []
    start = 0.0
    while start < duration:
        end = min(start + chunk_sec, duration)
        segs = [s for s in transcript.segments if s.end > start and s.start < end]
        if segs:
            text = "\n".join(f"[{s.start:.1f}-{s.end:.1f}] {s.text.strip()}" for s in segs)
            chunks.append(Chunk(start=start, end=end, text=text))
        if end >= duration:
            break
        start += step
    return chunks


# ---------------------------------------------------------------- LLM output


def _num(v: object, default: float | None) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def sanitize(raw: object, lo: float, hi: float) -> list[RawHighlight]:
    if not isinstance(raw, dict):
        return []
    items = raw.get("highlights")
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        s_raw = _num(it.get("start_time"), None)
        e_raw = _num(it.get("end_time"), None)
        if s_raw is None or e_raw is None or e_raw <= s_raw:
            continue
        s, e = max(lo, s_raw), min(hi, e_raw)
        if e <= s:
            continue
        score = int(max(0, min(100, _num(it.get("score"), 0) or 0)))
        out.append(
            RawHighlight(
                start=s,
                end=e,
                score=score,
                title=str(it.get("title") or "").strip()[:200],
                hook=str(it.get("hook_sentence") or "").strip()[:500],
                reason=str(it.get("virality_reason") or "").strip()[:500],
            )
        )
    return out


def ask_llm(
    llm: Any, system: str, prompt: str, lo: float, hi: float, max_tokens: int = 16000
) -> list[RawHighlight]:
    last = "unknown"
    for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
        p = prompt
        if attempt > 1:
            p += (
                "\n\nIMPORTANT: Return ONLY valid JSON with a top-level 'highlights' array. "
                "Each item: title, start_time, end_time, score, hook_sentence, virality_reason."
            )
        try:
            raw = llm.complete(system=system, prompt=p, max_tokens=max_tokens)
        except LLMError as e:
            if not e.retryable or attempt == MAX_LLM_ATTEMPTS:
                raise
            last = str(e)
            continue
        try:
            parsed = parse_json_loose(raw)
        except ValueError as e:
            last = f"invalid JSON: {e}"
            continue
        hs = sanitize(parsed, lo, hi)
        if hs:
            return hs
        last = "no valid highlights in response"
        if isinstance(parsed, dict) and parsed.get("highlights") == []:
            return []
    log.warning("select.llm_invalid_output", error=last)
    return []


# ---------------------------------------------------------------- boundaries


def _gap_before(words: list[Word], i: int) -> float:
    return words[i].start - words[i - 1].end if i > 0 else math.inf


def _gap_after(words: list[Word], i: int) -> float:
    return words[i + 1].start - words[i].end if i + 1 < len(words) else math.inf


def _ends_sentence(w: Word) -> bool:
    return w.text.rstrip().endswith(SENTENCE_END)


def _start_quality(words: list[Word], i: int) -> float:
    bonus = 0.0
    if _gap_before(words, i) >= PAUSE_SEC:
        bonus += 1.5
    if i > 0 and _ends_sentence(words[i - 1]):
        bonus += 1.0
    return bonus


def _end_quality(words: list[Word], i: int) -> float:
    bonus = 0.0
    if _gap_after(words, i) >= PAUSE_SEC:
        bonus += 1.5
    if _ends_sentence(words[i]):
        bonus += 1.0
    return bonus


def snap_start_index(words: list[Word], t: float) -> int:
    cands = [i for i, w in enumerate(words) if abs(w.start - t) <= SNAP_WINDOW]
    if not cands:
        after = [i for i, w in enumerate(words) if w.start >= t]
        return after[0] if after else len(words) - 1
    return min(cands, key=lambda i: (abs(words[i].start - t) - _start_quality(words, i), i))


def snap_end_index(words: list[Word], t: float, min_index: int) -> int:
    cands = [i for i, w in enumerate(words) if i >= min_index and abs(w.end - t) <= SNAP_WINDOW]
    if not cands:
        before = [i for i, w in enumerate(words) if i >= min_index and w.end <= t]
        return before[-1] if before else min_index
    return min(cands, key=lambda i: (abs(words[i].end - t) - _end_quality(words, i), -i))


def clip_start_time(words: list[Word], i: int) -> float:
    """Чуть раньше первого слова, но не захватывая предыдущее."""
    return round(max(0.0, words[i].start - min(0.15, _gap_before(words, i) / 2)), 3)


def clip_end_time(words: list[Word], j: int, duration: float) -> float:
    return round(min(duration, words[j].end + min(0.25, _gap_after(words, j) / 2)), 3)


def fit_duration(
    words: list[Word], i: int, j: int, min_sec: float, max_sec: float
) -> tuple[int, int] | None:
    """Подогнать конец (и при нужде начало) под [min_sec, max_sec] по границам слов."""
    start = words[i].start

    def dur(jj: int) -> float:
        return words[jj].end - start

    if dur(j) > max_sec:
        within = [k for k in range(i, j + 1) if dur(k) <= max_sec]
        if not within:
            return None
        good = [k for k in within if dur(k) >= min_sec and _end_quality(words, k) > 0]
        j = max(good) if good else max(within)
    if dur(j) < min_sec:
        longer = [k for k in range(j, len(words)) if min_sec <= dur(k) <= max_sec]
        if longer:
            good = [k for k in longer if _end_quality(words, k) > 0]
            j = min(good) if good else min(longer)
        else:
            # не хватает слов после — сдвигаем начало раньше
            earlier = [
                k for k in range(0, i) if min_sec <= words[j].end - words[k].start <= max_sec
            ]
            if not earlier:
                return None
            good = [k for k in earlier if _start_quality(words, k) > 0]
            i = max(good) if good else max(earlier)
    return i, j


def overlap_ratio(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = min(a[1], b[1]) - max(a[0], b[0])
    if inter <= 0:
        return 0.0
    return inter / min(a[1] - a[0], b[1] - b[0])


def dedupe(items: list[ClipCandidate]) -> list[ClipCandidate]:
    kept: list[ClipCandidate] = []
    for c in sorted(items, key=lambda c: (-c.score, c.start)):
        if all(overlap_ratio((c.start, c.end), (k.start, k.end)) <= DEDUPE_OVERLAP for k in kept):
            kept.append(c)
    return kept


def refine(
    raw: list[RawHighlight], transcript: Transcript, campaign: Campaign
) -> list[ClipCandidate]:
    words = transcript.words
    if not words:
        return []
    out = []
    for h in raw:
        i = snap_start_index(words, h.start)
        j = snap_end_index(words, h.end, i)
        fitted = fit_duration(words, i, j, campaign.clip_min_sec, campaign.clip_max_sec)
        if fitted is None:
            continue
        i, j = fitted
        start = clip_start_time(words, i)
        end = min(clip_end_time(words, j, transcript.duration), start + campaign.clip_max_sec)
        if end <= start:
            continue
        ident = hashlib.sha1(f"{start:.3f}-{end:.3f}".encode()).hexdigest()[:8]
        out.append(
            ClipCandidate(
                id=ident,
                start=start,
                end=end,
                score=h.score,
                hook=h.hook,
                reason=h.reason,
                title=h.title,
            )  # fmt: skip
        )
    return out


def select_highlights(
    transcript: Transcript,
    campaign: Campaign,
    llm: Any,
    *,
    chunk_sec: float,
    overlap_sec: float,
    max_tokens: int = 16000,
    sig: Any = None,
) -> list[ClipCandidate]:
    """sig (signals.Signals) — гибрид (ADR-0017): пики в промпте и вес сигнала в оценке."""
    template = Template(load_prompt("highlights.md"))
    system = template.substitute(
        clip_min_sec=int(campaign.clip_min_sec),
        clip_max_sec=int(campaign.clip_max_sec),
        max_candidates=max(campaign.clip_count * 2, 5),
        notes=campaign.notes.strip() or "(none)",
    )
    raw: list[RawHighlight] = []
    for chunk in build_chunks(transcript, chunk_sec, overlap_sec):
        prompt = (
            f"CHUNK_RANGE: {chunk.start:.2f}-{chunk.end:.2f}\n"
            f"Transcript (format: [start-end] text, seconds):\n{chunk.text}"
        )
        if sig is not None:
            prompt += peak_hint(sig, chunk.start, chunk.end)
        raw.extend(ask_llm(llm, system, prompt, chunk.start, chunk.end, max_tokens))
    refined = refine(raw, transcript, campaign)
    if sig is not None:
        refined = [rescore(c, sig) for c in refined]
    refined = dedupe(refined)
    top = sorted(refined, key=lambda c: (-c.score, c.start))[: campaign.clip_count]
    # id по хронологии: c01, c02... — стабильные и читаемые
    ordered = sorted(top, key=lambda c: c.start)
    return [c.model_copy(update={"id": f"c{n:02d}"}) for n, c in enumerate(ordered, 1)]


HYBRID_SIGNAL_SHARE = 0.3  # доля сигнала в итоговой оценке гибрида
WHOLE_MIN_SEC = 3.0  # selection: whole — короче не клип


def peak_hint(sig: Any, start: float, end: float) -> str:
    from clipfactory.pipeline import signals

    found = signals.peaks(sig, start, end)
    if not found:
        return ""
    lines = "\n".join(f"- {t:.1f}s strength {v:.2f} ({lead})" for t, v, lead in found)
    return (
        "\n\nAudience/engagement peaks in this range (audio spikes, chat bursts, "
        "most-replayed parts). Strongly prefer clips whose climax lands on one of them, "
        "but only if the speech around it also works as a clip:\n" + lines
    )


def rescore(cand: ClipCandidate, sig: Any) -> ClipCandidate:
    from clipfactory.pipeline import signals

    strength = signals.window_strength(sig, cand.start, cand.end)
    score = (1 - HYBRID_SIGNAL_SHARE) * cand.score + HYBRID_SIGNAL_SHARE * 100 * strength
    return cand.model_copy(update={"score": int(round(min(max(score, 0), 100)))})


class SelectStage:
    name = StageName.select
    version = 1

    def config(self, ctx: StageContext) -> dict[str, Any]:
        c = ctx.campaign
        if c.selection is SelectionMode.whole:
            return {
                "selection": "whole",
                "clip_min_sec": c.clip_min_sec,
                "clip_max_sec": c.clip_max_sec,
            }
        if c.selection is SelectionMode.signals:
            from clipfactory.pipeline import signals

            return {
                "selection": c.selection.value,
                "signals_version": signals.SIGNALS_VERSION,
                "weights": signals.WEIGHTS,
                "hop": signals.HOP,
                "clip_min_sec": c.clip_min_sec,
                "clip_max_sec": c.clip_max_sec,
                "clip_count": c.clip_count,
            }
        return {
            "llm": ctx.backends.identity("llm"),
            "prompt_sha": hashlib.sha256(load_prompt("highlights.md").encode()).hexdigest(),
            "clip_min_sec": c.clip_min_sec,
            "clip_max_sec": c.clip_max_sec,
            "clip_count": c.clip_count,
            "notes": c.notes,
            "chunk": [ctx.settings.chunk_seconds, ctx.settings.chunk_overlap_seconds],
            **self.hybrid_config(ctx),
        }

    @staticmethod
    def hybrid_config(ctx: StageContext) -> dict[str, Any]:
        if ctx.campaign.selection is not SelectionMode.hybrid:
            return {}  # ключи только для гибрида: кеш transcript-кампаний не меняется
        from clipfactory.pipeline import signals

        return {
            "selection": "hybrid",
            "signals_version": signals.SIGNALS_VERSION,
            "weights": signals.WEIGHTS,
            "signal_share": HYBRID_SIGNAL_SHARE,
        }

    def input_keys(self, ctx: StageContext) -> list[str]:
        keys = [ctx.key("transcript.json")]
        if ctx.campaign.selection is SelectionMode.whole:
            if ctx.storage.exists(ctx.key("source.info.json")):
                keys.append(ctx.key("source.info.json"))
        elif ctx.campaign.selection is not SelectionMode.transcript:
            keys.append(ctx.key("audio.wav"))
            for name in ("source.info.json", "chat.json"):
                if ctx.storage.exists(ctx.key(name)):
                    keys.append(ctx.key(name))
        return keys

    def run(self, ctx: StageContext) -> StageResult:
        transcript = ctx.read_model(ctx.key("transcript.json"), Transcript)
        if ctx.campaign.selection is SelectionMode.signals:
            return self._run_signals(ctx, transcript)
        if ctx.campaign.selection is SelectionMode.whole:
            return self._run_whole(ctx, transcript)
        sig = None
        outputs: list[str] = []
        if ctx.campaign.selection is SelectionMode.hybrid:
            sig, _ = self._signals(ctx)
            outputs.append(ctx.key("signals.json"))
        candidates = select_highlights(
            transcript,
            ctx.campaign,
            ctx.backends.llm,
            chunk_sec=ctx.settings.chunk_seconds,
            overlap_sec=ctx.settings.chunk_overlap_seconds,
            max_tokens=ctx.settings.llm_max_tokens,
            sig=sig,
        )
        if not candidates:
            raise NoHighlightsError("LLM returned no usable highlights for this video")
        key = ctx.key("highlights.json")
        ctx.write_model(key, Highlights(candidates=candidates))
        return StageResult(
            stage=self.name, outputs=[key, *outputs], info={"clips": len(candidates)}
        )

    def _signals(self, ctx: StageContext) -> tuple[Any, SourceInfo | None]:
        """Ряды сигналов + signals.json (пишется и при неудаче выбора — видно, почему)."""
        from clipfactory.pipeline import signals

        info_key = ctx.key("source.info.json")
        info = ctx.read_model(info_key, SourceInfo) if ctx.storage.exists(info_key) else None
        chat_key = ctx.key("chat.json")
        chat = ctx.read_model(chat_key, ChatActivity) if ctx.storage.exists(chat_key) else None
        sig = signals.build_signals(ctx.local_path(ctx.key("audio.wav")), info, chat=chat)
        ctx.write_model(ctx.key("signals.json"), signals.trace(sig))
        return sig, info

    def _run_signals(self, ctx: StageContext, transcript: Transcript) -> StageResult:
        """Без LLM: пики звука и «Most replayed» (ADR-0014). LLM пишет только метаданные."""
        from clipfactory.pipeline import signals

        sig, info = self._signals(ctx)
        trace_key = ctx.key("signals.json")
        duration = transcript.duration or sig.length * sig.hop
        picked = signals.pick_windows(
            sig, ctx.campaign, duration, words=transcript.words, info=info
        )
        top = sorted(dedupe(picked), key=lambda c: (-c.score, c.start))[: ctx.campaign.clip_count]
        if not top:
            raise NoHighlightsError(
                "no signal peaks found (quiet audio, no YouTube heatmap or video too short)"
            )
        ordered = sorted(top, key=lambda c: c.start)
        candidates = [c.model_copy(update={"id": f"c{n:02d}"}) for n, c in enumerate(ordered, 1)]
        key = ctx.key("highlights.json")
        ctx.write_model(key, Highlights(candidates=candidates))
        return StageResult(
            stage=self.name,
            outputs=[key, trace_key],
            info={"clips": len(candidates), "signals": sorted(sig.series)},
        )

    def _run_whole(self, ctx: StageContext, transcript: Transcript) -> StageResult:
        """Исходник — уже хайлайт (клип Twitch/YouTube): один клип, длиннее clip_max — обрезка."""
        info_key = ctx.key("source.info.json")
        info = ctx.read_model(info_key, SourceInfo) if ctx.storage.exists(info_key) else None
        duration = transcript.duration
        if duration < WHOLE_MIN_SEC:
            raise NoHighlightsError(f"source is too short for a clip ({duration:.1f}s)")
        end = min(duration, ctx.campaign.clip_max_sec)
        if end < duration and transcript.words:
            from clipfactory.pipeline import signals

            _, end = signals.snap_to_words(0.0, end, transcript.words)
            end = min(end, ctx.campaign.clip_max_sec, duration)
        title = info.title if info else ""
        cand = ClipCandidate(
            id="c01", start=0.0, end=round(end, 3), score=100, hook=title, title=title,
            reason="Whole source used as a clip (already a highlight)"
            + (f"; source title: {title}" if title else ""),
        )  # fmt: skip
        key = ctx.key("highlights.json")
        ctx.write_model(key, Highlights(candidates=[cand]))
        return StageResult(stage=self.name, outputs=[key], info={"clips": 1})

    def validate(self, ctx: StageContext, outputs: list[str]) -> None:
        h = ctx.read_model(ctx.key("highlights.json"), Highlights)
        if not h.candidates:
            raise ValidationFailed("highlights.json is empty")
        c = ctx.campaign
        lo = WHOLE_MIN_SEC if c.selection is SelectionMode.whole else c.clip_min_sec
        for cand in h.candidates:
            if not (lo - 0.5 <= cand.duration <= c.clip_max_sec + 0.5):
                raise ValidationFailed(f"clip {cand.id} duration {cand.duration:.1f}s out of range")
