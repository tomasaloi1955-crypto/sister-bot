"""Первая тренировка: сразу («Прямо сейчас» или время уже наступило) или в выбранное время."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.handlers.health import first_now

ALMATY = ZoneInfo("Asia/Almaty")


def u(slot: str, first: bool = False) -> dict:
    return {"slot": slot, "first_now": first, "tz": "Asia/Almaty"}


def at(hour: int) -> datetime:
    return datetime(2026, 11, 7, hour, 30, tzinfo=ALMATY).astimezone(timezone.utc)


def test_now_button_sends_immediately():
    assert first_now(u("day", first=True), at(8))
    assert first_now(u("evening", first=True), at(8))


def test_waits_for_chosen_time():
    assert not first_now(u("day"), at(8))        # 08:30, выбрала 11:00 — ждём
    assert not first_now(u("evening"), at(15))   # 15:30, выбрала 19:00 — ждём


def test_chosen_time_already_passed_sends_now():
    assert first_now(u("day"), at(12))
    assert first_now(u("evening"), at(20))
