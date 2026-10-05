"""Бот без сети: контроллер, admin-фильтр и aiogram Dispatcher на фейковой сессии."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    SendMessage,
    SendVideo,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from clipfactory.bot.handlers import BotController, ClipCard, Reply, Runtime, TrackJob, track_job
from clipfactory.bot.keyboards import parse_callback
from clipfactory.bot.main import BotConfigError, build_dispatcher
from clipfactory.pipeline.render import RenderStage
from clipfactory.schemas import ClipStatus, JobStatus
from tests.conftest import make_synthetic_video, needs_ffmpeg
from tests.helpers import make_fast_app

ADMIN, STRANGER = 111, 666
TOKEN = "123456:TEST-token-not-real"


class FakeSession(BaseSession):
    """Записывает вызовы Bot API и возвращает правдоподобные ответы. Никакой сети."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list = []
        self._mid = 100

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, SendMessage | SendVideo | EditMessageText):
            self._mid += 1
            return Message(
                message_id=self._mid,
                date=datetime.now(UTC),
                chat=Chat(id=getattr(method, "chat_id", ADMIN) or ADMIN, type="private"),
                text=getattr(method, "text", None),
                caption=getattr(method, "caption", None),
            )
        return True

    async def stream_content(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        pass

    def of(self, kind):
        return [r for r in self.requests if isinstance(r, kind)]


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    return Bot(TOKEN, session=session), session


def msg_update(uid: int, text: str, n: int = 1) -> Update:
    return Update(
        update_id=n,
        message=Message(
            message_id=n,
            date=datetime.now(UTC),
            chat=Chat(id=uid, type="private"),
            from_user=User(id=uid, is_bot=False, first_name="U"),
            text=text,
        ),
    )


def cb_update(uid: int, data: str, n: int = 2) -> Update:
    return Update(
        update_id=n,
        callback_query=CallbackQuery(
            id=str(n),
            from_user=User(id=uid, is_bot=False, first_name="U"),
            chat_instance="ci",
            data=data,
            message=Message(
                message_id=1, date=datetime.now(UTC), chat=Chat(id=uid, type="private"), text="x"
            ),
        ),
    )


def test_parse_callback():
    assert parse_callback("rv:a:20261005-221412-57009e:c01").action == "approve"
    assert parse_callback("camp:example").value == "example"
    assert parse_callback("retry:j1").kind == "retry"
    assert parse_callback("rv:zz:j:c") is None and parse_callback("evil") is None
    assert len("rv:a:20261005-221412-57009e:c01".encode()) <= 64


def test_dispatcher_requires_admins(tmp_path):
    app = make_fast_app(tmp_path, admin_ids=[])
    with pytest.raises(BotConfigError):
        build_dispatcher(app)


async def test_admin_only_and_url_flow(tmp_path):
    app = make_fast_app(tmp_path, admin_ids=[ADMIN])
    dp, ctl = build_dispatcher(app)
    bot, session = make_bot()

    await dp.feed_update(bot, msg_update(STRANGER, "https://youtu.be/abc"))
    assert session.requests == []  # чужому — тишина

    await dp.feed_update(bot, msg_update(ADMIN, "https://youtu.be/abc"))
    [sent] = session.of(SendMessage)
    assert "кампанию" in sent.text
    buttons = [b.callback_data for row in sent.reply_markup.inline_keyboard for b in row]
    assert "camp:fast" in buttons
    assert ctl.pending_source[ADMIN] == "https://youtu.be/abc"

    await dp.feed_update(bot, cb_update(STRANGER, "camp:fast"))
    assert len(session.requests) == 1 and app.db.list_jobs() == []
    await bot.session.close()


@needs_ffmpeg
@pytest.mark.slow
async def test_full_flow_progress_cards_review_and_retry(tmp_path, monkeypatch):
    src = make_synthetic_video(tmp_path / "v.mp4", duration=30)
    app = make_fast_app(tmp_path, admin_ids=[ADMIN])
    rt = Runtime(poll_interval=0.05)
    dp, ctl = build_dispatcher(app, rt)
    bot, session = make_bot()

    # render падает один раз — бот должен показать упавший этап и кнопку retry
    original, calls = RenderStage.run, {"n": 0}

    def flaky(self, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated render crash with token sk-ant-SECRET123")
        return original(self, ctx)

    monkeypatch.setattr(RenderStage, "run", flaky)

    await dp.feed_update(bot, msg_update(ADMIN, str(src)))
    await dp.feed_update(bot, cb_update(ADMIN, "camp:fast"))
    await asyncio.wait_for(asyncio.gather(*rt.tasks), timeout=120)

    [job] = app.db.list_jobs()
    assert app.db.get_job(job.id).status == JobStatus.failed
    texts = [m.text for m in session.of(SendMessage)] + [m.text for m in session.of(EditMessageText)]
    assert any("Упал этап <b>render</b>" in t for t in texts)
    assert not any("sk-ant-SECRET123" in t for t in texts)  # секреты не уходят в Telegram
    progress_msgs = [m for m in session.of(SendMessage) if "Job <code>" in m.text and "Загрузка" in m.text]
    assert len(progress_msgs) == 1  # прогресс — одно сообщение, дальше только edit
    retry_msg = session.of(SendMessage)[-1]
    assert retry_msg.reply_markup.inline_keyboard[0][0].callback_data == f"retry:{job.id}"

    session.requests.clear()
    await dp.feed_update(bot, cb_update(ADMIN, f"retry:{job.id}", n=3))
    await asyncio.wait_for(asyncio.gather(*rt.tasks), timeout=120)
    assert app.db.get_job(job.id).status == JobStatus.awaiting_review
    cards = session.of(SendVideo)
    assert len(cards) == 2
    assert cards[0].reply_markup.inline_keyboard[0][0].callback_data.startswith("rv:a:")
    assert session.of(AnswerCallbackQuery)

    clip_id = app.db.list_clips(job.id)[0].clip_id
    await dp.feed_update(bot, cb_update(ADMIN, f"rv:a:{job.id}:{clip_id}", n=4))
    assert app.db.get_clip(job.id, clip_id).status == ClipStatus.approved

    # правка метаданных: кнопка -> текст из трёх строк
    await dp.feed_update(bot, cb_update(ADMIN, f"rv:e:{job.id}:{clip_id}", n=5))
    await dp.feed_update(bot, msg_update(ADMIN, "Свой заголовок\nСвоё описание\n#один #два", n=6))
    metas = ctl.review.effective_meta(job.id, clip_id)
    assert all(m.title == "Свой заголовок" for m in metas)
    assert [a["action"] for a in app.db.review_actions(job.id)] == ["approve", "edit_metadata"]
    await bot.session.close()


def test_controller_outputs_without_aiogram(tmp_path):
    app = make_fast_app(tmp_path, admin_ids=[ADMIN])
    ctl = BotController(app)
    [r] = ctl.on_text(ADMIN, "привет")
    assert isinstance(r, Reply) and "Не понял" in r.text
    [r] = ctl.on_callback(ADMIN, "camp:fast")
    assert "Сначала" in r.text
    outs = ctl.on_text(ADMIN, "https://example.com/v.mp4")
    outs = ctl.on_callback(ADMIN, "camp:fast")
    assert isinstance(outs[1], TrackJob) and outs[1].start is not None
    assert ctl.on_callback(ADMIN, "rv:a:nojob:c01")[0].text.startswith("Не получилось")
    assert not any(isinstance(o, ClipCard) for o in ctl.clip_cards("nojob"))


async def test_track_job_without_start_stops_on_terminal(tmp_path):
    app = make_fast_app(tmp_path, admin_ids=[ADMIN])
    job = app.create_job("https://example.com/v.mp4", "fast")
    app.db.set_job_failed(job.id, None, "x", "boom", False)
    bot, session = make_bot()
    await asyncio.wait_for(
        track_job(bot, ADMIN, TrackJob(job.id), BotController(app), Runtime(poll_interval=0.01)), 5
    )
    assert session.of(SendMessage)[-1].reply_markup is not None  # кнопка retry
    await bot.session.close()
