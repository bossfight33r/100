from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from clipfactory.publish.scheduler import MIN_GAP, SchedulingError, plan_slots
from clipfactory.schemas import Account

MSK = ZoneInfo("Europe/Moscow")


def acc(**kw) -> Account:
    base = dict(id="yt", platform="youtube", name="YT", daily_limit=2,
                posting_windows=["09:00-12:00", "18:00-22:00"], timezone="Europe/Moscow")  # fmt: skip
    base.update(kw)
    return Account(**base)


def in_windows(slot: datetime, a: Account) -> bool:
    local = slot.astimezone(ZoneInfo(a.timezone)).strftime("%H:%M")
    return any(w.split("-")[0] <= local <= w.split("-")[1] for w in a.posting_windows)


def test_slots_respect_windows_limit_and_gap():
    a = acc()
    now = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)  # 10:00 MSK
    slots = plan_slots(a, 7, now=now)
    assert len(slots) == 7
    assert all(s >= now + timedelta(minutes=20) for s in slots)
    assert all(in_windows(s, a) for s in slots)
    per_day = Counter(s.astimezone(MSK).date() for s in slots)
    assert max(per_day.values()) <= a.daily_limit
    ordered = sorted(slots)
    assert all(b - a_ >= MIN_GAP for a_, b in zip(ordered, ordered[1:], strict=False))


def test_existing_publications_count_towards_daily_limit():
    a = acc(daily_limit=2)
    now = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)  # 07:00 MSK
    existing = [datetime(2026, 10, 5, 6, 0, tzinfo=UTC), datetime(2026, 10, 5, 15, 0, tzinfo=UTC)]
    [slot] = plan_slots(a, 1, now=now, existing=existing)
    assert slot.astimezone(MSK).date() == datetime(2026, 10, 6).date()


def test_daily_limit_uses_account_timezone_not_utc():
    # 23:30 MSK 5-го = 20:30 UTC 5-го; окно до 23:59 — сутки считаются по Москве
    a = acc(daily_limit=1, posting_windows=["00:30-23:59"])
    now = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)
    slots = plan_slots(a, 2, now=now)
    days = [s.astimezone(MSK).date() for s in slots]
    assert days[0] != days[1]


def test_dst_gap_skipped():
    # Нью-Йорк, 8 марта 2026: 02:00–03:00 не существует
    a = acc(timezone="America/New_York", posting_windows=["02:00-03:30"], daily_limit=3)
    now = datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    slots = plan_slots(a, 1, now=now)
    local = slots[0].astimezone(ZoneInfo("America/New_York"))
    assert local.hour >= 3 or local.date() > datetime(2026, 3, 8).date()


def test_zero_limit_and_overflow():
    with pytest.raises(SchedulingError):
        plan_slots(acc(daily_limit=0), 1, now=datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(SchedulingError):
        plan_slots(acc(daily_limit=1), 61, now=datetime(2026, 1, 1, tzinfo=UTC))
