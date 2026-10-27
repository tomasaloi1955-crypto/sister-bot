"""Расписание воронки: что положено отправить участнице прямо сейчас.

Здесь только расчёт, без базы и Telegram — поэтому логику легко проверить
тестами (tests/test_funnel.py). Отправляет tick.py.

Каждое сообщение имеет ключ (workout:3:day, sale:7 …). Ключ пишется в
таблицу sends перед отправкой, поэтому одно и то же сообщение не уйдёт
дважды, даже если тики пришли подряд или одновременно.

Сообщение дня уходит, если сейчас у участницы этот день и уже наступил его
час (по её часовому поясу), но ещё не наступил «тихий» час. Пропустил
сервер день целиком — сообщения того дня не догоняем (тренировки
устаревают, а пачка старых сообщений раздражает).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config

# Бесплатная неделя: 3 тренировки через день (между ними — дни отдыха).
WORKOUT_DAYS = {1: 1, 3: 2, 5: 3}        # день воронки → номер тренировки
WORKOUTS = len(WORKOUT_DAYS)
CHECKIN_DAYS = (5,)                      # «Как ощущения?» вечером после 3-й тренировки
NUDGE_DAYS = {4: 2}                      # день → к этой тренировке пора было нажать «Сделала»
WARM_DAYS = {4: "warm_reviews", 6: "warm_stats"}
SALE_DAYS = (7, 8, 9)                    # предложение, возражения, последний день
POST_DAYS = (12, 16, 24)
PERIODIC_EVERY = 14                      # после 24-го дня — раз в 2 недели


@dataclass
class Person:
    tg_id: int
    started_at: datetime
    tz: str = config.DEFAULT_TZ
    slot: str = "evening"                # day (11:00) | evening (19:00)
    paid: bool = False
    last_done_day: int = 0               # номер последней тренировки с «Сделала» (0 — ни разу)


@dataclass
class Action:
    key: str                             # уникальный ключ для таблицы sends
    kind: str                            # workout | checkin | nudge | warm_* | sale | post
    day: int                             # день воронки; у workout — номер тренировки
    slot: str | None = None
    extra: dict = field(default_factory=dict)


def zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except Exception:
        return ZoneInfo(config.DEFAULT_TZ)


def funnel_day(started_at: datetime, now: datetime, tz: str) -> int:
    """День воронки: день анкеты = 1, следующий календарный день = 2 …"""
    z = zone(tz)
    return (now.astimezone(z).date() - started_at.astimezone(z).date()).days + 1


def slots_of(slot: str | None) -> list[str]:
    """Тренировка одна на день; slot — только время, когда её прислать."""
    return ["day"] if slot == "day" else ["evening"]


def workouts_received(d: int) -> int:
    """Сколько тренировок участница получила к дню воронки d."""
    return sum(1 for day in WORKOUT_DAYS if day <= d)


def slot_hour(slot: str) -> int:
    return config.DAY_HOUR if slot == "day" else config.EVENING_HOUR


def plan(p: Person, now: datetime) -> list[Action]:
    """Все сообщения, которые участнице положены на текущий момент."""
    local = now.astimezone(zone(p.tz))
    hour = local.hour
    if hour >= config.QUIET_HOUR:
        return []
    d = funnel_day(p.started_at, now, p.tz)
    out: list[Action] = []

    # Тренировка своего дня; в день отдыха после неё — та же ещё раз под тем же
    # ключом: если вчера она не ушла (видео не было, сервер спал), уйдёт сегодня,
    # а если ушла — журнал sends не даст прислать второй раз.
    for wd in (d, d - 1):
        if wd in WORKOUT_DAYS and (wd == d or d not in WORKOUT_DAYS):
            for s in slots_of(p.slot):
                if hour >= slot_hour(s):
                    out.append(Action(f"workout:{wd}:{s}", "workout", WORKOUT_DAYS[wd], s))
    if d in CHECKIN_DAYS and hour >= config.CHECKIN_HOUR:
        out.append(Action(f"checkin:{d}", "checkin", d))
    if d in NUDGE_DAYS and p.last_done_day < NUDGE_DAYS[d] and hour >= config.NUDGE_HOUR:
        out.append(Action(f"nudge:{d}", "nudge", d))

    if p.paid or hour < config.INFO_HOUR:
        return out
    if d in WARM_DAYS:
        out.append(Action(f"{WARM_DAYS[d]}:{d}", WARM_DAYS[d], d))
    elif d in SALE_DAYS:
        out.append(Action(f"sale:{d}", "sale", d))
    elif d in POST_DAYS or (d > POST_DAYS[-1] and (d - POST_DAYS[-1]) % PERIODIC_EVERY == 0):
        out.append(Action(f"post:{d}", "post", d))
    return out

