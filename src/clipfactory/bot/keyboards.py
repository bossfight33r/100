from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from clipfactory.schemas import Campaign

# callback_data <= 64 байт: "rv:a:<job_id 22>:<clip_id>" ~ 32 байта
REVIEW_ACTIONS = {
    "a": "approve",
    "r": "reject",
    "e": "edit",
    "c": "captions",
    "k": "crop",
}


@dataclass(frozen=True)
class Callback:
    kind: str  # camp | rv | retry | clips | pick
    action: str = ""
    job_id: str = ""
    clip_id: str = ""
    value: str = ""


def parse_callback(data: str) -> Callback | None:
    parts = data.split(":")
    if parts[0] == "camp" and len(parts) == 2:
        return Callback(kind="camp", value=parts[1])
    if parts[0] == "rv" and len(parts) == 4 and parts[1] in REVIEW_ACTIONS:
        return Callback(
            kind="rv", action=REVIEW_ACTIONS[parts[1]], job_id=parts[2], clip_id=parts[3]
        )
    if parts[0] == "pick" and len(parts) == 2 and parts[1].isdigit():
        return Callback(kind="pick", value=parts[1])
    if parts[0] in ("retry", "clips") and len(parts) == 2:
        return Callback(kind=parts[0], job_id=parts[1])
    return None


def campaigns_kb(campaigns: list[Campaign]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=c.name, callback_data=f"camp:{c.id}")] for c in campaigns]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def clip_kb(job_id: str, clip_id: str) -> InlineKeyboardMarkup:
    def b(text: str, code: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=f"rv:{code}:{job_id}:{clip_id}")

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [b("✅ Одобрить", "a"), b("❌ Отклонить", "r")],
            [b("✏️ Метаданные", "e")],
            [b("🔤 Субтитры заново", "c"), b("🎯 Кроп", "k")],
        ]
    )


def retry_kb(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 Повторить с упавшего этапа", callback_data=f"retry:{job_id}"
                )
            ]
        ]
    )


def discover_kb(count: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=str(i), callback_data=f"pick:{i}") for i in range(1, count + 1)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i : i + 5] for i in range(0, count, 5)])
