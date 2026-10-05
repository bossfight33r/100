"""Планирование слотов публикации по окнам, timezone и дневному лимиту аккаунта."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from clipfactory.schemas import Account

MIN_GAP = timedelta(minutes=90)  # между публикациями одного аккаунта
LEAD = timedelta(minutes=20)  # YouTube publishAt должен быть в будущем; запас на загрузку
HORIZON_DAYS = 60


class SchedulingError(Exception):
    pass


def _windows(account: Account) -> list[tuple[time, time]]:
    out = []
    for w in account.posting_windows:
        a, b = w.split("-")
        out.append((time.fromisoformat(a), time.fromisoformat(b)))
    return sorted(out)


def _local_day(dt: datetime, tz: ZoneInfo) -> date:
    return dt.astimezone(tz).date()


def plan_slots(
    account: Account,
    count: int,
    *,
    now: datetime,
    existing: Iterable[datetime] = (),
    min_gap: timedelta = MIN_GAP,
    lead: timedelta = LEAD,
) -> list[datetime]:
    """Ближайшие ``count`` слотов (UTC) для аккаунта.

    Гарантии: слот внутри posting window по локальному времени аккаунта; не больше
    daily_limit публикаций в календарные сутки аккаунта (с учётом уже запланированных);
    не ближе ``min_gap`` к другим публикациям аккаунта; не раньше ``now + lead``.
    """
    if count <= 0:
        return []
    if account.daily_limit <= 0:
        raise SchedulingError(f"account {account.id} has daily_limit=0")
    tz = ZoneInfo(account.timezone)
    taken = sorted(e.astimezone(UTC) for e in existing)
    per_day = Counter(_local_day(e, tz) for e in taken)
    earliest = (now + lead).astimezone(UTC)
    result: list[datetime] = []
    day = _local_day(earliest, tz)
    for _ in range(HORIZON_DAYS):
        for start, end in _windows(account):
            # слоты в окне с шагом min_gap от начала окна
            cursor = datetime.combine(day, start, tzinfo=tz)
            window_end = datetime.combine(day, end, tzinfo=tz)
            while cursor <= window_end:
                if per_day[day] >= account.daily_limit:
                    break
                slot = cursor.astimezone(UTC)
                ok = slot >= earliest and all(abs(slot - t) >= min_gap for t in taken)
                # несуществующее локальное время при переходе на летнее время пропускаем
                if ok and slot.astimezone(tz).replace(tzinfo=None) == cursor.replace(tzinfo=None):
                    result.append(slot)
                    taken.append(slot)
                    per_day[day] += 1
                    if len(result) == count:
                        return result
                cursor = (cursor.astimezone(UTC) + min_gap).astimezone(tz)
        day += timedelta(days=1)
    raise SchedulingError(
        f"could not fit {count} publications for {account.id} within {HORIZON_DAYS} days"
    )
