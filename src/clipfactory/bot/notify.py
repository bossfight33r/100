"""Тексты прогресса и карточек. Без сетевых вызовов — их делает handlers."""

from __future__ import annotations

from typing import Any

from clipfactory.log import redact_text
from clipfactory.schemas import STAGE_ORDER, STAGE_STATUS, ClipRecord, JobStatus, PlatformClipMeta

TERMINAL = (JobStatus.awaiting_review, JobStatus.failed, JobStatus.published, JobStatus.scheduled)
STAGE_LABELS = {
    "ingest": "Загрузка",
    "transcribe": "Транскрипция",
    "select": "Выбор моментов",
    "reframe": "Кроп 9:16",
    "captions": "Субтитры",
    "render": "Рендер",
}


def safe_error(message: str | None, limit: int = 600) -> str:
    """Ошибка для Telegram: без секретов и без гигантского stderr."""
    text = redact_text(message or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def progress_text(app: Any, job_id: str) -> str:
    job = app.db.get_job(job_id)
    runs = app.db.stage_runs(job_id)
    last: dict[str, dict] = {}
    for r in runs:
        last[r["stage"]] = r
    current_stage = next((s for s, st in STAGE_STATUS.items() if st == job.status), None)
    lines = [f"🎬 Job <code>{job.id}</code> · кампания <b>{job.campaign_id}</b>"]
    for stage in STAGE_ORDER:
        r = last.get(stage.value)
        if job.status == JobStatus.failed and job.failed_stage == stage:
            icon = "❌"
        elif r and r["status"] in ("completed", "skipped"):
            icon = "♻️" if r["cached"] else "✅"
        elif stage == current_stage:
            icon = "⏳"
        else:
            icon = "▫️"
        lines.append(f"{icon} {STAGE_LABELS[stage.value]}")
    if job.status == JobStatus.failed:
        lines.append(
            f"\nУпал этап <b>{job.failed_stage.value if job.failed_stage else '?'}</b> "
            f"({job.error_type}, retryable={job.retryable})\n<pre>{_html(safe_error(job.error_message))}</pre>"
        )
    elif job.status == JobStatus.awaiting_review:
        lines.append("\nГотово — клипы ниже, жду ревью.")
    return "\n".join(lines)


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clip_caption(clip: ClipRecord, metas: list[PlatformClipMeta]) -> str:
    status = {
        "pending_review": "⏳ ждёт ревью",
        "approved": "✅ одобрен",
        "rejected": "❌ отклонён",
    }
    lines = [
        f"<b>{clip.clip_id}</b> · {clip.end - clip.start:.0f} с · score {clip.score} · "
        f"{status.get(clip.status.value, clip.status.value)}",
        f"Хук: {_html(clip.hook[:200])}",
    ]
    for m in metas:
        block = (
            f"\n<b>{m.platform.value}</b>: {_html(m.title[:120])}\n{_html(m.description[:250])}\n"
            f"{_html(' '.join(m.hashtags)[:150])}"
        )
        # лимит подписи Telegram 1024; режем целыми блоками, чтобы не порвать HTML-теги
        if len("\n".join([*lines, block])) > 1000:
            lines.append("\n…")
            break
        lines.append(block)
    return "\n".join(lines)
