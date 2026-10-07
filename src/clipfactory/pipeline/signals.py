"""Выбор моментов по сигналам, без речи (ADR-0014): игры, стримы, турниры.

Каждый сигнал — ряд оценок 0..1 с шагом HOP секунд:
- audio: всплеск громкости относительно локальной базы (выстрелы, крик, рёв зала);
- heatmap: YouTube «Most replayed» — где зрители пересматривают;
- chat: всплеск сообщений в чате записи стрима, сдвинутый на задержку реакции (ADR-0016).
Ряды смешиваются с весами доступных сигналов, вокруг пиков строятся окна
длиной target, пик кладётся ближе к концу окна (развязка после завязки).
"""

from __future__ import annotations

import hashlib
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from clipfactory.schemas import (
    Campaign,
    ChatActivity,
    ClipCandidate,
    SignalsTrace,
    SourceInfo,
    Word,
)

SIGNALS_VERSION = 3
HOP = 0.5  # сек на точку ряда
BASELINE_SEC = 60.0  # окно скользящей медианы громкости
SMOOTH_SEC = 2.0  # сглаживание: одиночный щелчок — не момент
AUDIO_RANGE_DB = 12.0  # превышение базы, дающее оценку 1.0
AUDIO_DEADBAND_DB = 1.5  # колебания фона — не всплеск
SILENCE_DB = -70.0
PEAK_POSITION = 0.65  # где в окне клипа стоит пик
MIN_PEAK = 0.08  # ниже — фон, а не момент
WEIGHTS = {"heatmap": 0.6, "chat": 0.5, "audio": 0.4}
CHAT_LAG_SEC = 6.0  # зрители пишут после момента
CHAT_SMOOTH_SEC = 6.0
CHAT_BASELINE_SEC = 120.0
CHAT_MIN_MESSAGES = 50  # меньше — шум, а не сигнал
WORD_PAD = 0.1


@dataclass(frozen=True)
class Signals:
    hop: float
    series: dict[str, np.ndarray]  # имя -> оценки 0..1, одинаковой длины

    @property
    def length(self) -> int:
        return len(next(iter(self.series.values()))) if self.series else 0

    def fused(self, weights: dict[str, float] = WEIGHTS) -> np.ndarray:
        total = sum(weights.get(k, 0.0) for k in self.series)
        if not self.series or total <= 0:
            return np.zeros(self.length)
        acc = np.zeros(self.length)
        for k, s in self.series.items():
            acc += weights.get(k, 0.0) * s
        return acc / total


# ---------------------------------------------------------------- series


def rms_db(wav_path: Path, hop: float = HOP) -> np.ndarray:
    """Громкость (dBFS RMS) по окнам hop секунд; WAV PCM 16-bit, читается кусками."""
    with wave.open(str(wav_path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("expected 16-bit PCM WAV")
        rate, channels = w.getframerate(), w.getnchannels()
        frames_per_hop = max(int(rate * hop), 1)
        out: list[float] = []
        while True:
            raw = w.readframes(frames_per_hop * 600)
            if not raw:
                break
            x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
            if channels > 1:
                x = x[: len(x) // channels * channels].reshape(-1, channels).mean(axis=1)
            for i in range(0, len(x), frames_per_hop):
                seg = x[i : i + frames_per_hop]
                if len(seg) < frames_per_hop // 2 and out:
                    break  # хвост короче половины окна
                rms = float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0
                out.append(20 * np.log10(rms) if rms > 1e-6 else -120.0)
    return np.maximum(np.asarray(out), -120.0)


def _moving(fn, x: np.ndarray, width: int) -> np.ndarray:
    if width <= 1 or len(x) == 0:
        return x.copy()
    half = width // 2
    padded = np.pad(x, (half, width - 1 - half), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, width)
    return fn(windows, axis=1)


def audio_scores(db: np.ndarray, hop: float = HOP) -> np.ndarray:
    """Всплеск над скользящей медианой, сглаженный; 0..1. Тишина не даёт оценки."""
    if len(db) == 0:
        return db
    baseline = _moving(np.median, db, int(BASELINE_SEC / hop) | 1)
    smooth = _moving(np.mean, db, max(int(SMOOTH_SEC / hop), 1))
    excess = np.clip((smooth - baseline - AUDIO_DEADBAND_DB) / AUDIO_RANGE_DB, 0.0, 1.0)
    excess[smooth < SILENCE_DB] = 0.0
    return excess


def heatmap_scores(info: SourceInfo, length: int, hop: float = HOP) -> np.ndarray | None:
    """Кривая «Most replayed» на сетке hop; нормировка: медиана -> 0, максимум -> 1."""
    if not info.heatmap or length == 0:
        return None
    raw = np.zeros(length)
    for p in info.heatmap:
        a = max(int(p.start_time / hop), 0)
        b = min(max(int(np.ceil(p.end_time / hop)), a + 1), length)
        raw[a:b] = np.maximum(raw[a:b], p.value)
    covered = raw[raw > 0]
    if covered.size == 0:
        return None
    med, top = float(np.median(covered)), float(covered.max())
    if top - med < 1e-6:
        return None  # плоская кривая — сигнала нет
    return np.clip((raw - med) / (top - med), 0.0, 1.0)


def chat_scores(chat: ChatActivity, length: int, hop: float = HOP) -> np.ndarray | None:
    """Темп чата над скользящей медианой, сдвинутый раньше на CHAT_LAG_SEC; 0..1."""
    counts = np.asarray(chat.counts, dtype=np.float64)
    if length == 0 or counts.sum() < CHAT_MIN_MESSAGES:
        return None
    rate = counts / chat.hop
    t = (np.arange(length) + 0.5) * hop + CHAT_LAG_SEC  # реакция на момент t — в t + лаг
    idx = np.minimum((t / chat.hop).astype(int), len(rate) - 1)
    series = rate[idx]
    series[t >= len(rate) * chat.hop] = 0.0
    smooth = _moving(np.mean, series, max(int(CHAT_SMOOTH_SEC / hop), 1))
    baseline = _moving(np.median, smooth, int(CHAT_BASELINE_SEC / hop) | 1)
    excess = smooth - baseline
    positive = excess[excess > 0]
    if positive.size == 0:
        return None
    scale = float(np.percentile(positive, 95))
    return np.clip(excess / scale, 0.0, 1.0) if scale > 0 else None


def build_signals(
    wav_path: Path | None,
    info: SourceInfo | None,
    hop: float = HOP,
    chat: ChatActivity | None = None,
) -> Signals:
    series: dict[str, np.ndarray] = {}
    if wav_path is not None:
        series["audio"] = audio_scores(rms_db(wav_path, hop), hop)
    length = len(series["audio"]) if series else 0
    if not length and info is not None and info.duration:
        length = int(info.duration / hop)
    if info is not None:
        hm = heatmap_scores(info, length, hop)
        if hm is not None:
            series["heatmap"] = hm
    if chat is not None:
        cs = chat_scores(chat, length, hop)
        if cs is not None:
            series["chat"] = cs
    return Signals(hop=hop, series=series)


def trace(sig: Signals, step: float = 1.0, weights: dict[str, float] = WEIGHTS) -> SignalsTrace:
    """Ряды с шагом step (максимум внутри шага), 3 знака — компактно для signals.json."""
    k = max(int(round(step / sig.hop)), 1)

    def down(x: np.ndarray) -> list[float]:
        if x.size == 0:
            return []
        pad = (-len(x)) % k
        padded = np.pad(x, (0, pad), mode="edge") if pad else x
        return [round(float(v), 3) for v in padded.reshape(-1, k).max(axis=1)]

    return SignalsTrace(
        hop=sig.hop * k,
        weights={n: weights.get(n, 0.0) for n in sig.series},
        series={n: down(s) for n, s in sorted(sig.series.items())},
        fused=down(sig.fused(weights)),
    )


def peaks(
    sig: Signals,
    start: float = 0.0,
    end: float = float("inf"),
    *,
    max_n: int = 8,
    min_gap: float = 20.0,
    weights: dict[str, float] = WEIGHTS,
) -> list[tuple[float, float, str]]:
    """Пики итогового ряда в [start, end): (время, сила 0..1, ведущий сигнал)."""
    fused = sig.fused(weights)
    if fused.size == 0:
        return []
    a = max(int(start / sig.hop), 0)
    b = min(int(end / sig.hop) if end != float("inf") else fused.size, fused.size)
    work = fused[a:b].copy()
    gap = max(int(min_gap / sig.hop), 1)
    out = []
    while len(out) < max_n and work.size:
        i = int(np.argmax(work))
        v = float(work[i])
        if v < MIN_PEAK:
            break
        idx = a + i
        lead = max(sig.series, key=lambda k: weights.get(k, 0.0) * float(sig.series[k][idx]))
        out.append(((idx + 0.5) * sig.hop, v, lead))
        work[max(i - gap, 0) : i + gap] = -1.0
    return sorted(out)


def window_strength(
    sig: Signals, start: float, end: float, weights: dict[str, float] = WEIGHTS
) -> float:
    """Сила сигналов внутри клипа 0..1: 0.7·пик + 0.3·среднее, по максимуму ряда."""
    fused = sig.fused(weights)
    if fused.size == 0:
        return 0.0
    best = float(fused.max())
    if best <= 0:
        return 0.0
    a = max(int(start / sig.hop), 0)
    b = max(min(int(np.ceil(end / sig.hop)), fused.size), a + 1)
    w = fused[a:b]
    if w.size == 0:
        return 0.0
    return (0.7 * float(w.max()) + 0.3 * float(w.mean())) / best


# ---------------------------------------------------------------- windows


def target_duration(campaign: Campaign) -> float:
    """Игровой момент короче мысли в подкасте: четверть диапазона от минимума."""
    lo, hi = campaign.clip_min_sec, campaign.clip_max_sec
    return lo + (hi - lo) * 0.25


def snap_to_words(start: float, end: float, words: list[Word]) -> tuple[float, float]:
    """Не резать слово пополам: граница внутри слова уходит на его край."""
    for w in words:
        if w.start < start < w.end:
            start = max(0.0, w.start - WORD_PAD)
        if w.start < end < w.end:
            end = w.end + WORD_PAD
    return start, end


def _chapter_title(info: SourceInfo | None, t: float) -> str:
    if info is None:
        return ""
    for c in info.chapters:
        if c.start_time <= t < c.end_time:
            return c.title
    return ""


def pick_windows(
    signals: Signals,
    campaign: Campaign,
    duration: float,
    *,
    words: list[Word] | None = None,
    info: SourceInfo | None = None,
    weights: dict[str, float] = WEIGHTS,
) -> list[ClipCandidate]:
    fused = signals.fused(weights)
    if fused.size == 0 or duration <= 0:
        return []
    target = min(target_duration(campaign), duration)
    if target < campaign.clip_min_sec - 0.01:
        return []  # видео короче минимального клипа
    hop = signals.hop
    work = fused.copy()
    best = float(fused.max()) or 1.0
    picked: list[ClipCandidate] = []
    while len(picked) < campaign.clip_count * 2:
        i = int(np.argmax(work))
        peak = float(work[i])
        if peak < MIN_PEAK:
            break
        t = (i + 0.5) * hop
        start = min(max(t - PEAK_POSITION * target, 0.0), duration - target)
        end = start + target
        if words:
            start, end = snap_to_words(start, end, words)
            end = min(end, start + campaign.clip_max_sec, duration)
        a, b = int(start / hop), max(int(np.ceil(end / hop)), int(start / hop) + 1)
        window = fused[a:b]
        score = 100 * (0.7 * peak + 0.3 * float(window.mean())) / best
        # окно и полклипа вокруг гасим: следующие кандидаты не пересекаются
        pad = int(target / 2 / hop)
        work[max(a - pad, 0) : b + pad] = -1.0
        if end - start < campaign.clip_min_sec - 0.5:
            continue
        parts = [f"{k} {float(s[i]):.2f}" for k, s in sorted(signals.series.items())]
        chapter = _chapter_title(info, t)
        ident = hashlib.sha1(f"{start:.3f}-{end:.3f}".encode()).hexdigest()[:8]
        picked.append(
            ClipCandidate(
                id=ident,
                start=round(start, 3),
                end=round(end, 3),
                score=int(round(min(max(score, 0), 100))),
                hook=chapter,
                reason=f"Signal peak at {t:.1f}s ({', '.join(parts)})"
                + (f"; chapter: {chapter}" if chapter else "")
                + (f"; source video: {info.title}" if info and info.title else ""),
                title=chapter or (info.title if info else ""),
            )
        )
    return picked
