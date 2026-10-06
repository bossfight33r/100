"""Telegram control plane.

BotController — вся логика без aiogram и сети (тестируется напрямую).
build_router — тонкая aiogram-обвязка: превращает Out-объекты в вызовы Bot API.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from clipfactory.bot.keyboards import campaigns_kb, clip_kb, parse_callback, retry_kb
from clipfactory.bot.notify import TERMINAL, _html, clip_caption, progress_text, safe_error
from clipfactory.config import ConfigError
from clipfactory.db import NotFound
from clipfactory.log import get_logger
from clipfactory.pipeline.ingest import is_url
from clipfactory.pipeline.review import ReviewError, ReviewService
from clipfactory.schemas import JobStatus

log = get_logger(__name__)

URL_RE = re.compile(r"^https?://\S+$")


# ---------------------------------------------------------------- outputs


@dataclass
class Reply:
    text: str
    keyboard: InlineKeyboardMarkup | None = None


@dataclass
class ClipCard:
    video_path: Path
    thumb_path: Path | None
    caption: str
    keyboard: InlineKeyboardMarkup


@dataclass
class TrackJob:
    """Сигнал обвязке: запустить job в фоне и вести прогресс одним сообщением."""

    job_id: str
    start: Callable[[], Any] | None = None


Out = Reply | ClipCard | TrackJob


# ---------------------------------------------------------------- access


class AdminMiddleware(BaseMiddleware):
    """Пропускает только ADMIN_IDS. Остальным — тишина (не раскрываем существование бота)."""

    def __init__(self, admin_ids: list[int]) -> None:
        self.admin_ids = set(admin_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id not in self.admin_ids:
            log.warning("bot.denied", user_id=getattr(user, "id", None))
            return None
        return await handler(event, data)


# ---------------------------------------------------------------- controller


HELP = (
    "Пришлите видеофайл, ссылку (YouTube или прямой URL) или абсолютный путь к файлу на этой машине.\n"
    "Потом выберите кампанию — я нарежу клипы и пришлю их на ревью.\n\n"
    "/jobs — последние job\n/status JOB_ID — статус и клипы\n"
    "/publish JOB_ID — запланировать и опубликовать одобренные клипы\n"
    "/stats — просмотры и доход\n/cancel JOB_ID — остановить job"
)


@dataclass
class BotController:
    app: Any
    pending_source: dict[int, str] = field(default_factory=dict)
    pending_edit: dict[int, tuple[str, str]] = field(default_factory=dict)
    pending_crop: dict[int, tuple[str, str]] = field(default_factory=dict)

    @property
    def review(self) -> ReviewService:
        return ReviewService(self.app)

    # ------------------------------------------------------------ messages

    def help(self) -> list[Out]:
        return [Reply(HELP)]

    def on_source(self, user_id: int, source: str) -> list[Out]:
        campaigns = list(self.app.settings.campaigns.values())
        if not campaigns:
            return [Reply("Нет ни одной кампании в config/campaigns/.")]
        self.pending_source[user_id] = source
        return [Reply("Выберите кампанию:", campaigns_kb(campaigns))]

    def on_text(self, user_id: int, text: str) -> list[Out]:
        text = text.strip()
        if user_id in self.pending_edit:
            return self._apply_edit(user_id, text)
        if user_id in self.pending_crop:
            return self._apply_crop(user_id, text)
        if URL_RE.match(text):
            return self.on_source(user_id, text)
        path = Path(text).expanduser()
        if path.is_absolute() and path.is_file():
            return self.on_source(user_id, str(path))
        return [Reply("Не понял. " + HELP)]

    def _apply_edit(self, user_id: int, text: str) -> list[Out]:
        job_id, clip_id = self.pending_edit.pop(user_id)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return [Reply("Пусто — правка отменена.")]
        title = lines[0]
        description = lines[1] if len(lines) > 1 else None
        hashtags = lines[2].split() if len(lines) > 2 else None
        try:
            self.review.edit_metadata(
                job_id, clip_id, title=title, description=description,
                hashtags=hashtags, actor=f"tg:{user_id}",
            )  # fmt: skip
        except ReviewError as e:
            return [Reply(f"Не получилось: {safe_error(str(e))}")]
        return [Reply(f"Метаданные {clip_id} обновлены."), *self.clip_cards(job_id, [clip_id])]

    def _apply_crop(self, user_id: int, text: str) -> list[Out]:
        job_id, clip_id = self.pending_crop.pop(user_id)
        center: float | None
        if text.lower() in ("auto", "авто"):
            center = None
        else:
            try:
                center = float(text.replace("%", "").replace(",", ".")) / 100
            except ValueError:
                return [Reply("Нужно число 0–100 или auto. Правка отменена.")]
        try:
            stage = self.review.rerender_crop(
                job_id, clip_id, center_x=center, actor=f"tg:{user_id}", run=False
            )
        except ReviewError as e:
            return [Reply(f"Не получилось: {safe_error(str(e))}")]
        return [self._rerun(job_id, stage)]

    def _rerun(self, job_id: str, stage: Any) -> TrackJob:
        return TrackJob(job_id, start=lambda: self.app.enqueue_job(job_id, force_stage=stage))

    # ------------------------------------------------------------ callbacks

    def on_callback(self, user_id: int, data: str) -> list[Out]:
        cb = parse_callback(data)
        if cb is None:
            return [Reply("Неизвестная кнопка.")]
        try:
            if cb.kind == "camp":
                return self._start_job(user_id, cb.value)
            if cb.kind == "retry":
                return self._retry(cb.job_id)
            if cb.kind == "clips":
                return self.clip_cards(cb.job_id)
            return self._review(user_id, cb.action, cb.job_id, cb.clip_id)
        except (ReviewError, NotFound, ValueError, ConfigError) as e:
            return [Reply(f"Не получилось: {safe_error(str(e))}")]

    def _start_job(self, user_id: int, campaign_id: str) -> list[Out]:
        source = self.pending_source.pop(user_id, None)
        if source is None:
            return [Reply("Сначала пришлите видео или ссылку.")]
        job = self.app.create_job(source, campaign_id)
        return [
            Reply(f"Job <code>{job.id}</code> создан."),
            TrackJob(job.id, start=lambda: self.app.enqueue_job(job.id)),
        ]

    def _retry(self, job_id: str) -> list[Out]:
        job = self.app.db.get_job(job_id)
        if job.status != JobStatus.failed:
            return [Reply(f"Job {job_id} не в статусе failed ({job.status.value}).")]
        return [TrackJob(job_id, start=lambda: self.app.retry_job(job_id))]

    def _review(self, user_id: int, action: str, job_id: str, clip_id: str) -> list[Out]:
        actor = f"tg:{user_id}"
        if action == "approve":
            self.review.approve(job_id, clip_id, actor=actor)
            return [Reply(f"✅ {clip_id} одобрен.")]
        if action == "reject":
            self.review.reject(job_id, clip_id, actor=actor)
            return [Reply(f"❌ {clip_id} отклонён — публиковаться не будет.")]
        if action == "edit":
            self.review.effective_meta(job_id, clip_id)  # проверка, что клип есть
            self.pending_edit[user_id] = (job_id, clip_id)
            return [
                Reply(
                    f"Правка {clip_id}. Пришлите:\n1-я строка — заголовок\n"
                    "2-я (опц.) — описание\n3-я (опц.) — хэштеги через пробел"
                )
            ]
        if action == "captions":
            stage = self.review.rerender_captions(job_id, clip_id, actor=actor, run=False)
            return [self._rerun(job_id, stage)]
        if action == "crop":
            self.pending_crop[user_id] = (job_id, clip_id)
            return [Reply(f"Кроп {clip_id}: пришлите центр в % ширины кадра (0–100) или auto.")]
        return [Reply("Неизвестное действие.")]

    # ------------------------------------------------------------ views

    def clip_cards(self, job_id: str, only: list[str] | None = None) -> list[Out]:
        out: list[Out] = []
        for clip in self.app.db.list_clips(job_id):
            if only and clip.clip_id not in only:
                continue
            if not clip.video_key or not self.app.storage.exists(clip.video_key):
                continue
            metas = self.review.effective_meta(job_id, clip.clip_id)
            thumb = (
                self.app.storage.local_path(clip.thumb_key)
                if clip.thumb_key and self.app.storage.exists(clip.thumb_key)
                else None
            )
            out.append(
                ClipCard(
                    video_path=self.app.storage.local_path(clip.video_key),
                    thumb_path=thumb,
                    caption=clip_caption(clip, metas),
                    keyboard=clip_kb(job_id, clip.clip_id),
                )
            )
        return out

    def status(self, job_id: str) -> list[Out]:
        try:
            text = progress_text(self.app, job_id)
        except NotFound:
            return [Reply("Нет такого job.")]
        job = self.app.db.get_job(job_id)
        kb = retry_kb(job_id) if job.status == JobStatus.failed else None
        return [Reply(text, kb)]

    def jobs(self) -> list[Out]:
        jobs = self.app.db.list_jobs(limit=10)
        if not jobs:
            return [Reply("Job ещё нет.")]
        lines = [f"<code>{j.id}</code> · {j.campaign_id} · {j.status.value}" for j in jobs]
        return [Reply("\n".join(lines))]

    def publish(self, job_id: str) -> list[Out]:
        """Запланировать и опубликовать одобренные клипы (блокирующий вызов — в потоке)."""
        from clipfactory.pipeline.publish import PublishService, PublishServiceError
        from clipfactory.publish.scheduler import SchedulingError

        try:
            svc = PublishService(self.app)
            svc.schedule_job(job_id)
            svc.publish_job(job_id)
        except (PublishServiceError, SchedulingError, NotFound) as e:
            return [Reply(f"Не получилось: {safe_error(str(e))}")]
        lines = []
        for p in self.app.db.list_publications(job_id=job_id):
            when = p.scheduled_at.strftime("%d.%m %H:%M UTC") if p.scheduled_at else "-"
            lines.append(f"{p.clip_id} → {p.account_id}: {when} [{p.status.value}]")
        return [Reply("\n".join(lines) or "Нечего публиковать.")]

    def cancel(self, job_id: str) -> list[Out]:
        try:
            result = self.app.cancel_job(job_id)
        except (NotFound, ValueError) as e:
            return [Reply(f"Не получилось: {safe_error(str(e))}")]
        return [Reply("Снят из очереди." if result == "dequeued" else "Отмена запрошена.")]

    def stats(self) -> list[Out]:
        from clipfactory.track.report import build_report, render_text

        text = render_text(build_report(self.app, top=5))
        return [Reply(f"<pre>{text.replace('&', '&amp;').replace('<', '&lt;')[:3800]}</pre>")]

    def after_job(self, job_id: str) -> list[Out]:
        """Что отправить, когда job дошёл до терминального статуса."""
        job = self.app.db.get_job(job_id)
        if job.status == JobStatus.failed:
            return [Reply("Можно повторить:", retry_kb(job_id))]
        if job.status == JobStatus.awaiting_review:
            return self.clip_cards(job_id)
        return []


# ---------------------------------------------------------------- aiogram glue


@dataclass
class Runtime:
    """Фоновые задачи бота; держим ссылки, чтобы их не собрал GC."""

    poll_interval: float = 3.0
    tasks: set[asyncio.Task] = field(default_factory=set)

    def spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task


async def send_outs(
    bot: Bot, chat_id: int, outs: list[Out], ctl: BotController, rt: Runtime
) -> None:
    for o in outs:
        if isinstance(o, Reply):
            await bot.send_message(chat_id, o.text, reply_markup=o.keyboard, parse_mode="HTML")
        elif isinstance(o, ClipCard):
            await bot.send_video(
                chat_id,
                FSInputFile(o.video_path),
                caption=o.caption,
                parse_mode="HTML",
                reply_markup=o.keyboard,
                thumbnail=FSInputFile(o.thumb_path) if o.thumb_path else None,
                width=1080,
                height=1920,
                supports_streaming=True,
            )
        elif isinstance(o, TrackJob):
            rt.spawn(track_job(bot, chat_id, o, ctl, rt))


async def track_job(bot: Bot, chat_id: int, tj: TrackJob, ctl: BotController, rt: Runtime) -> None:
    """Запустить job (в потоке — pipeline синхронный) и обновлять одно сообщение прогресса."""
    app = ctl.app
    msg = await bot.send_message(chat_id, progress_text(app, tj.job_id), parse_mode="HTML")
    runner: asyncio.Future | None = None
    if tj.start is not None:
        runner = asyncio.ensure_future(asyncio.to_thread(tj.start))
    last = msg.html_text if msg.text else ""
    while True:
        await asyncio.sleep(rt.poll_interval)
        text = progress_text(app, tj.job_id)
        if text != last:
            try:
                await bot.edit_message_text(
                    text, chat_id=chat_id, message_id=msg.message_id, parse_mode="HTML"
                )
                last = text
            except Exception as e:  # «message is not modified» и сетевые сбои не роняют трекер
                log.debug("bot.edit_failed", error=str(e))
        status = app.db.get_job(tj.job_id).status
        runner_done = runner is None or runner.done()
        if status in TERMINAL and runner_done:
            break
        if runner is not None and runner.done() and runner.exception() is not None:
            # запуск упал до того, как pipeline записал статус (Redis недоступен и т.п.)
            await bot.send_message(
                chat_id,
                f"Не удалось запустить job: {_html(safe_error(str(runner.exception())))}",
            )
            return
    if runner is not None and runner.exception() is not None:
        log.warning("bot.job_runner_error", job_id=tj.job_id, error=str(runner.exception()))
    await send_outs(bot, chat_id, ctl.after_job(tj.job_id), ctl, rt)


def build_router(ctl: BotController, rt: Runtime, inbox: Path) -> Router:
    router = Router(name="clipfactory")

    @router.message(Command("start", "help"))
    async def _help(message: Message, bot: Bot) -> None:
        await send_outs(bot, message.chat.id, ctl.help(), ctl, rt)

    @router.message(Command("jobs"))
    async def _jobs(message: Message, bot: Bot) -> None:
        await send_outs(bot, message.chat.id, ctl.jobs(), ctl, rt)

    @router.message(Command("status"))
    async def _status(message: Message, bot: Bot, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Использование: /status JOB_ID")
            return
        await send_outs(bot, message.chat.id, ctl.status(command.args.strip()), ctl, rt)

    @router.message(Command("cancel"))
    async def _cancel(message: Message, bot: Bot, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Использование: /cancel JOB_ID")
            return
        await send_outs(bot, message.chat.id, ctl.cancel(command.args.strip()), ctl, rt)

    @router.message(Command("stats"))
    async def _stats(message: Message, bot: Bot) -> None:
        await send_outs(bot, message.chat.id, await asyncio.to_thread(ctl.stats), ctl, rt)

    @router.message(Command("publish"))
    async def _publish(message: Message, bot: Bot, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Использование: /publish JOB_ID")
            return
        outs = await asyncio.to_thread(ctl.publish, command.args.strip())
        await send_outs(bot, message.chat.id, outs, ctl, rt)

    @router.message(F.video | F.document)
    async def _file(message: Message, bot: Bot) -> None:
        media = message.video or message.document
        name = getattr(media, "file_name", None) or f"{media.file_unique_id}.mp4"
        dest = inbox / f"{media.file_unique_id}{Path(name).suffix or '.mp4'}"
        inbox.mkdir(parents=True, exist_ok=True)
        try:
            await bot.download(media, destination=dest)
        except Exception as e:
            await message.answer(
                "Не удалось скачать файл (лимит Bot API — 20 МБ). Пришлите ссылку или путь к файлу.\n"
                + safe_error(str(e), 200)
            )
            return
        await send_outs(
            bot, message.chat.id, ctl.on_source(message.from_user.id, str(dest)), ctl, rt
        )

    @router.message(F.text)
    async def _text(message: Message, bot: Bot) -> None:
        outs = ctl.on_text(message.from_user.id, message.text or "")
        await send_outs(bot, message.chat.id, outs, ctl, rt)

    @router.callback_query()
    async def _callback(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        outs = ctl.on_callback(query.from_user.id, query.data or "")
        chat_id = query.message.chat.id if query.message else query.from_user.id
        await send_outs(bot, chat_id, outs, ctl, rt)

    return router


def source_kind(source: str) -> str:
    return "url" if is_url(source) else "file"
