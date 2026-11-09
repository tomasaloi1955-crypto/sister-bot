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


def test_club_records_only_6x():
    def deliveries(plan):
        return [funnel.club_keys(START, "Asia/Almaty", plan, "day", at(d, 12)) for d in range(1, 15)]
    assert all(ks == [] for ks in deliveries("3x"))                 # 3x — записи по дням /live (tick.run_live)
    six = deliveries("6x")
    # 1 октября 2026 — чт (отдых). Тренировки вт ср пт сб вс; в пн и чт — досылка
    # вчерашней, если не ушла; в день вступления, если он отдых, — ничего
    # чт1 пт2 сб3 вс4 пн5 вт6 ср7 чт8
    assert six[:8] == [[], ["club:1:day"], ["club:2:day"], ["club:3:day"], ["club:3:day"],
                       ["club:5:day"], ["club:6:day"], ["club:6:day"]]
    assert {k for ks in six for k in ks} == {f"club:{i}:day" for i in (1, 2, 3, 5, 6, 8, 9, 10, 12, 13)}
    # дни эфиров (ср пт вс) идут через run_live — здесь остаются записи вт и сб
    ld = frozenset({2, 4, 6})
    rec = [funnel.club_keys(START, "Asia/Almaty", "6x", "day", at(d, 12), ld) for d in range(1, 15)]
    # чт1 пт2 сб3 вс4 пн5 вт6 ср7 чт8 пт9 сб10
    assert rec[:10] == [[], [], ["club:2:day"], [], [], ["club:5:day"], [], [], [], ["club:9:day"]]
    assert funnel.rec_days_text("6x", ld) == "Вт, Сб"
    assert not funnel.club_delivers_on(START, "Asia/Almaty", "6x", at(2, 12), live_days=ld)   # пт — эфир
    assert funnel.club_delivers_on(START, "Asia/Almaty", "6x", at(3, 12), live_days=ld)       # сб
    assert funnel.club_keys(START, "Asia/Almaty", "6x", "day", at(2, 10)) == []        # до 11:00 — рано
    assert funnel.club_keys(START, "Asia/Almaty", "6x", "day", at(2, 22)) == []        # тихий час
    assert funnel.club_delivers_on(START, "Asia/Almaty", "6x", at(2, 12))               # пт
    assert not funnel.club_delivers_on(START, "Asia/Almaty", "6x", at(5, 12))           # пн — отдых
    assert not funnel.club_delivers_on(START, "Asia/Almaty", "6x", at(4, 12), ahead=1)  # завтра пн
    assert funnel.club_delivers_on(START, "Asia/Almaty", "6x", at(5, 12), ahead=1)      # завтра вт
    assert not funnel.club_delivers_on(START, "Asia/Almaty", "3x", at(1, 12))


def test_parse_live():
    assert funnel.parse_days("пн ср пт 19:00") == {0, 2, 4}
    assert funnel.parse_days("Вторник, четверг, суббота") == {1, 3, 5}
    assert funnel.parse_days("19:00") is None
    assert funnel.parse_time("19:00") == (19, 0)
    assert funnel.parse_time("9.30") == (9, 30)
    assert funnel.parse_time("25:00") is None
    assert funnel.parse_time("пн") is None


def test_live_actions():
    live = funnel.Live(days=frozenset({0, 2, 4}), hour=19)
    fri = 2                                                  # день 2 = пятница 2 октября

    def kinds(now, mode=None, started=False):
        return [k for _, k, _ in funnel.live_actions(live, now, mode, started)]
    # по умолчанию — по записи: запись с утреннего часа, без напоминаний об эфире
    assert kinds(at(fri, 9)) == []                           # рано
    assert funnel.live_actions(live, at(fri, 10))[0][:2] == ("liverec:2026-10-02", "rec")
    assert kinds(at(fri, 18)) == ["rec"]
    assert kinds(at(fri, 18), mode="rec") == ["rec"]
    assert kinds(at(fri, 22)) == []                          # тихий час
    assert kinds(at(1, 18)) == []                            # четверг — тренировки нет
    # тренер объявила эфир — напоминания, а не начался — запись
    L = "live"
    assert kinds(at(fri, 9), L) == []
    key, kind, _ = funnel.live_actions(live, at(fri, 10), L)[0]
    assert (key, kind) == ("live:2026-10-02:day", "day")
    assert kinds(at(fri, 18), L) == ["soon"]
    assert kinds(at(fri, 19, 10), L) == ["soon"]
    assert kinds(at(fri, 19, 10), L, started=True) == ["soon"]
    assert kinds(at(fri, 19, 16), L) == ["soon", "rec"]      # эфир не начался — запись
    assert kinds(at(fri, 20), L) == ["rec"]
    assert kinds(at(fri, 20), L, started=True) == []         # эфир идёт — запись не нужна
    assert kinds(at(fri, 22), L) == []                       # тихий час
    # отпуск и отмена дня не убирают — в такой день всем запись
    off = funnel.Live(days=live.days, hour=19, on=False)
    skipped = funnel.Live(days=live.days, hour=19, skip=frozenset({"2026-10-02"}))
    today = funnel.live_today(live, at(fri, 12))
    assert funnel.live_cancelled(off, today, None)
    assert funnel.live_cancelled(skipped, today, None)
    assert funnel.live_cancelled(live, today, "rec")
    assert not funnel.live_cancelled(live, today, None)
    assert not funnel.live_cancelled(live, today, "live")
    assert funnel.next_live(skipped, at(fri, 12)).day == 5       # следующий — пн 5 октября
    assert funnel.next_live(live, at(fri, 12)).day == 2

def test_club_repeat_cycle():
    from app.messages import next_repeat
    assert next_repeat([1, 2, 3, 4], 5, 0) == 1
    assert next_repeat([1, 2, 3, 4], 5, 1) == 2
    assert next_repeat([1, 2, 3, 4], 5, 4) == 1          # по кругу
    assert next_repeat([1, 2, 7], 3, 2) == 1             # только пройденные (< следующей)
    assert next_repeat([], 1, 0) is None
    # с бесплатной неделей: сначала она (-1, -2, -3), потом пройденные клуба, по кругу
    assert next_repeat([1, 2], 3, 0, {1, 2, 3}) == -1
    assert next_repeat([1, 2], 3, -1, {1, 2, 3}) == -2
    assert next_repeat([1, 2], 3, -3, {1, 2, 3}) == 1
    assert next_repeat([1, 2], 3, 2, {1, 2, 3}) == -1        # по кругу
    assert next_repeat([], 1, 0, {1, 2, 3}) == -1            # клуба ещё нет — только бесплатные
