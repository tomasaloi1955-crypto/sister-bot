"""Расписание воронки: python -m pytest tests  (из корня хранилища)."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import funnel

ALMATY = ZoneInfo("Asia/Almaty")
START = datetime(2026, 10, 1, 9, 30, tzinfo=ALMATY)        # анкета пройдена 1 октября


def at(day: int, hour: int, minute: int = 5) -> datetime:
    """Момент дня воронки `day` в `hour` по Алматы (в UTC, как приходит с сервера)."""
    local = datetime(2026, 10, 1, hour, minute, tzinfo=ALMATY) + timedelta(days=day - 1)
    return local.astimezone(timezone.utc)


def keys(p: funnel.Person, now: datetime) -> set[str]:
    return {a.key for a in funnel.plan(p, now)}


def person(**kw) -> funnel.Person:
    base = dict(tg_id=1, started_at=START, tz="Asia/Almaty", slot="evening", last_done_day=0)
    base.update(kw)
    return funnel.Person(**base)


def test_funnel_day_uses_local_date():
    assert funnel.funnel_day(START, at(1, 23), "Asia/Almaty") == 1
    assert funnel.funnel_day(START, at(2, 0, 10), "Asia/Almaty") == 2


def test_three_workouts_every_other_day():
    p = person(slot="evening", last_done_day=3)
    sent = {d: [a for a in funnel.plan(p, at(d, 19)) if a.kind == "workout"] for d in range(1, 10)}
    # В дни отдыха 2, 4, 6 — та же тренировка под тем же ключом (досылка пропущенной).
    assert {d: [a.day for a in acts] for d, acts in sent.items() if acts} == \
        {1: [1], 2: [1], 3: [2], 4: [2], 5: [3], 6: [3]}
    assert keys(p, at(3, 19)) == {"workout:3:evening"}
    assert keys(p, at(2, 19)) == {"workout:1:evening"}


def test_workout_only_after_slot_hour():
    p = person(slot="day", last_done_day=1)
    assert keys(p, at(3, 10)) == set()
    assert keys(p, at(3, 11)) == {"workout:3:day"}


def test_quiet_hour_sends_nothing():
    assert keys(person(), at(5, 22)) == set()


def test_checkin_after_third_workout():
    p = person(last_done_day=3)
    assert "checkin:5" in keys(p, at(5, 20))
    assert not any(k.startswith("checkin") for k in keys(p, at(5, 19)))


def test_nudge_if_second_workout_not_done():
    assert "nudge:4" in keys(person(last_done_day=1), at(4, 17))
    assert "nudge:4" in keys(person(last_done_day=0), at(4, 17))
    assert not any(k.startswith("nudge") for k in keys(person(last_done_day=2), at(4, 17)))
    assert not any(k.startswith("nudge") for k in keys(person(last_done_day=0), at(4, 16)))


def test_warmup_and_sale_days():
    assert "warm_reviews:4" in keys(person(last_done_day=2), at(4, 13))
    assert "warm_stats:6" in keys(person(last_done_day=3), at(6, 13))
    for d in (7, 8, 9):
        assert keys(person(last_done_day=3), at(d, 13)) == {f"sale:{d}"}


def test_paid_gets_no_selling():
    for d in (4, 6, 7, 8, 9, 12, 16, 24, 38):
        ks = keys(person(paid=True, last_done_day=3), at(d, 13))
        assert not any(k.split(":")[0] in ("sale", "post") or k.startswith("warm") for k in ks), (d, ks)


def test_after_free_week():
    p = person(last_done_day=3)
    assert keys(p, at(10, 13)) == set()
    for d in (12, 16, 24, 38, 52):
        assert keys(p, at(d, 13)) == {f"post:{d}"}
    assert keys(p, at(31, 13)) == set()


def test_workouts_received():
    assert [funnel.workouts_received(d) for d in range(1, 8)] == [1, 1, 2, 2, 3, 3, 3]


def test_bad_timezone_falls_back():
    assert funnel.funnel_day(START, at(3, 12), "Mars/Base") == 3
