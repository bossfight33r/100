from __future__ import annotations

import asyncio
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from clipfactory.bot.handlers import AdminMiddleware, BotController, Runtime, build_router
from clipfactory.log import get_logger

log = get_logger(__name__)


class BotConfigError(Exception):
    pass


def build_dispatcher(app: Any, rt: Runtime | None = None) -> tuple[Dispatcher, BotController]:
    if not app.settings.admin_ids:
        raise BotConfigError("CF_ADMIN_IDS is empty — the bot would answer nobody")
    rt = rt or Runtime()
    ctl = BotController(app)
    dp = Dispatcher()
    admin = AdminMiddleware(app.settings.admin_ids)
    dp.message.outer_middleware(admin)
    dp.callback_query.outer_middleware(admin)
    dp.include_router(build_router(ctl, rt, app.settings.data_dir / "inbox"))
    return dp, ctl


async def run_bot(app: Any) -> None:  # pragma: no cover - сеть
    token = app.settings.telegram_bot_token
    if token is None or not token.get_secret_value():
        raise BotConfigError("TELEGRAM_BOT_TOKEN is not set")
    dp, _ = build_dispatcher(app)
    bot = Bot(token.get_secret_value(), default=DefaultBotProperties(parse_mode="HTML"))
    log.info("bot.start", admins=len(app.settings.admin_ids))
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()


def main(app: Any) -> None:  # pragma: no cover
    asyncio.run(run_bot(app))
