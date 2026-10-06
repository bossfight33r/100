"""Отчёты: кампании, аккаунты, топ-клипы, эффективность хуков, рекомендации к промпту.

Production-промпт автоматически НЕ меняется: рекомендации пишутся в отдельный
файл data/reports/ для ручного ревью.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from clipfactory.track.earnings import strategy_for

NUMBER_RE = re.compile(r"\d")
FIRST_PERSON_RE = re.compile(r"\b(я|мне|меня|мой|моя|i|my|me)\b", re.I)
CURIOSITY_RE = re.compile(
    r"(никто|секрет|правд|ошиб|никогда|nobody|secret|truth|mistake|never)", re.I
)


class Row(BaseModel):
    key: str
    label: str
    publications: int
    views: int
    likes: int
    comments: int
    earnings: Decimal


class ClipRow(BaseModel):
    publication_id: str
    job_id: str
    clip_id: str
    account_id: str
    platform: str
    hook: str
    score: int
    views: int
    earnings: Decimal
    url: str | None


class HookFeature(BaseModel):
    feature: str
    clips: int
    avg_views: float
    lift: float | None  # avg_views с признаком / без; None — нет группы сравнения


class Report(BaseModel):
    generated_at: datetime
    total_views: int
    total_earnings: Decimal
    campaigns: list[Row]
    accounts: list[Row]
    top_clips: list[ClipRow]
    hook_features: list[HookFeature]
    score_views_correlation: float | None


def hook_features(hook: str) -> dict[str, bool]:
    h = hook.strip()
    words = h.split()
    return {
        "вопрос": h.endswith("?"),
        "число в хуке": bool(NUMBER_RE.search(h)),
        "первое лицо": bool(FIRST_PERSON_RE.search(h)),
        "интрига (секрет/никто/ошибка)": bool(CURIOSITY_RE.search(h)),
        "короткий (≤6 слов)": 0 < len(words) <= 6,
    }


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    return round(statistics.correlation(xs, ys), 3)


def build_report(app: Any, campaign_id: str | None = None, top: int = 10) -> Report:
    db = app.db
    latest = db.latest_stats()
    pubs = db.list_publications(campaign_id=campaign_id)
    clips: list[ClipRow] = []
    camp_rows: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(int))
    acc_rows: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(int))
    for pub in pubs:
        try:
            campaign = app.settings.campaign(pub.campaign_id)
        except Exception:  # кампанию удалили из конфига — статистика остаётся, доход 0
            campaign = None
        snap = latest.get(pub.id)
        earning = (
            strategy_for(campaign).compute(campaign, pub, snap).amount if campaign else Decimal(0)
        )
        views = snap.views if snap else 0
        for rows, key in ((camp_rows, pub.campaign_id), (acc_rows, pub.account_id)):
            r = rows[key]
            r["publications"] += 1
            r["views"] += views
            r["likes"] += snap.likes if snap else 0
            r["comments"] += snap.comments if snap else 0
            r["earnings"] = r.get("earnings", Decimal(0)) + earning
        try:
            clip = db.get_clip(pub.job_id, pub.clip_id)
            hook, score = clip.hook, clip.score
        except Exception:
            hook, score = "", 0
        clips.append(
            ClipRow(
                publication_id=pub.id,
                job_id=pub.job_id,
                clip_id=pub.clip_id,
                account_id=pub.account_id,
                platform=pub.platform.value,
                hook=hook,
                score=score,
                views=views,
                earnings=earning,
                url=pub.url,
            )  # fmt: skip
        )

    def rows(src: dict[str, dict[str, Any]], labels: dict[str, str]) -> list[Row]:
        out = [
            Row(
                key=k,
                label=labels.get(k, k),
                publications=v["publications"],
                views=v["views"],
                likes=v["likes"],
                comments=v["comments"],
                earnings=v.get("earnings", Decimal(0)),
            )  # fmt: skip
            for k, v in src.items()
        ]
        return sorted(out, key=lambda r: (-r.earnings, -r.views, r.key))

    camp_labels = {c.id: c.name for c in app.settings.campaigns.values()}
    acc_labels = {a.id: a.name for a in app.settings.accounts.values()}
    with_stats = [c for c in clips if c.publication_id in latest]
    features: list[HookFeature] = []
    if with_stats:
        names = list(hook_features("").keys())
        for name in names:
            yes = [c.views for c in with_stats if hook_features(c.hook)[name]]
            no = [c.views for c in with_stats if not hook_features(c.hook)[name]]
            if not yes:
                continue
            avg_yes = statistics.fmean(yes)
            avg_no = statistics.fmean(no) if no else 0.0
            features.append(
                HookFeature(
                    feature=name,
                    clips=len(yes),
                    avg_views=round(avg_yes, 1),
                    lift=round(avg_yes / avg_no, 2) if no and avg_no else None,
                )  # fmt: skip
            )
        features.sort(key=lambda f: -(f.lift or 0))
    return Report(
        generated_at=datetime.now(UTC),
        total_views=sum(c.views for c in clips),
        total_earnings=sum((c.earnings for c in clips), Decimal(0)),
        campaigns=rows(camp_rows, camp_labels),
        accounts=rows(acc_rows, acc_labels),
        top_clips=sorted(clips, key=lambda c: (-c.views, c.publication_id))[:top],
        hook_features=features,
        score_views_correlation=_pearson(
            [float(c.score) for c in with_stats], [float(c.views) for c in with_stats]
        ),
    )


def render_text(r: Report) -> str:
    lines = [
        f"Отчёт на {r.generated_at:%Y-%m-%d %H:%M} UTC",
        f"Всего: {r.total_views} просмотров, доход ${r.total_earnings}",
        "",
        "Кампании:",
    ]
    lines += [
        f"  {x.label}: {x.publications} публ., {x.views} просм., ${x.earnings}" for x in r.campaigns
    ] or ["  —"]
    lines += ["", "Аккаунты:"]
    lines += [
        f"  {x.label}: {x.publications} публ., {x.views} просм., {x.likes} лайков, ${x.earnings}"
        for x in r.accounts
    ] or ["  —"]
    lines += ["", "Топ клипов:"]
    lines += [
        f"  {c.views:>8} просм.  ${c.earnings}  {c.account_id}/{c.clip_id}  «{c.hook[:60]}»"
        for c in r.top_clips
    ] or ["  —"]
    if r.hook_features:
        lines += ["", "Хуки (lift = средние просмотры с признаком / без):"]
        lines += [
            f"  {f.feature}: {f.clips} клипов, ср. {f.avg_views:.0f}, "
            f"lift {f.lift if f.lift is not None else 'н/д'}"
            for f in r.hook_features
        ]
    if r.score_views_correlation is not None:
        lines.append(f"\nКорреляция score LLM ↔ просмотры: {r.score_views_correlation}")
    return "\n".join(lines)


def write_prompt_recommendations(
    app: Any, r: Report, out_dir: Path | None = None, campaign_id: str | None = None
) -> Path:
    """Файл с наблюдениями и примерами для ручной правки prompts/highlights.md."""
    out_dir = out_dir or app.settings.data_dir / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"prompt_recommendations_{r.generated_at:%Y%m%d_%H%M}.md"
    # только публикации со снятой статистикой: 0 просмотров у запланированных/упавших
    # публикаций — отсутствие данных, а не слабый хук
    tracked = app.db.latest_stats()
    with_hooks = [c for c in r.top_clips if c.hook and c.publication_id in tracked]
    all_clips = sorted(
        (
            c
            for c in build_report(app, campaign_id=campaign_id, top=10_000).top_clips
            if c.hook and c.publication_id in tracked
        ),
        key=lambda c: c.views,
    )
    worst = all_clips[:5]
    lines = [
        "# Рекомендации к промпту выбора моментов",
        "",
        f"Сгенерировано {r.generated_at:%Y-%m-%d %H:%M} UTC. **Промпт не изменён** — правь",
        "`src/clipfactory/prompts/highlights.md` вручную, если согласен.",
        "",
        "## Что работает в хуках",
    ]
    rated = [f for f in r.hook_features if f.lift is not None and f.clips >= 2]
    helpful = [f for f in rated if f.lift >= 1.2]
    harmful = [f for f in rated if f.lift <= 0.8]
    lines += [
        f"- «{f.feature}»: lift {f.lift} на {f.clips} клипах — усилить в промпте" for f in helpful
    ]
    lines += [f"- «{f.feature}»: lift {f.lift} — ослабить/убрать акцент" for f in harmful]
    if not helpful and not harmful:
        lines.append(
            "- Данных пока мало для выводов (нужно ≥ 2 клипов на признак и заметный lift)."
        )
    if r.score_views_correlation is not None:
        lines += [
            "",
            f"Корреляция score модели и просмотров: {r.score_views_correlation} "
            + (
                "— score полезен."
                if r.score_views_correlation > 0.3
                else "— score слабо предсказывает просмотры; стоит пересмотреть критерии."
            ),
        ]
    lines += ["", "## Лучшие хуки (примеры для few-shot)"]
    lines += [f"- {c.views} просм.: «{c.hook}»" for c in with_hooks[:5]] or ["- —"]
    lines += ["", "## Слабые хуки (антипримеры)"]
    lines += [f"- {c.views} просм.: «{c.hook}»" for c in worst] or ["- —"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
