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
from datetime import datetime, timedelta
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



# ─────────────────────────────── клуб ───────────────────────────────
# Тариф 3x «3 раза в неделю» — 3 тренировки в неделю (дни и время — /live): обычно
# живой эфир в группе клуба (бот напоминает), а если эфира не будет — бот лично
# присылает тренировку в записи. Тариф 6x — эфиров нет: бот лично присылает
# тренировку в записи 6 дней из 7 (неделя — от дня покупки), видео удаляется
# через MEDIA_TTL_HOURS.
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_DAY_NAMES = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
REST_WEEKDAY = 6                                        # воскресенье


@dataclass
class Live:
    days: frozenset = frozenset({0, 2, 4})              # 0 — понедельник
    hour: int = 19
    minute: int = 0
    on: bool = True                                     # /live off — пауза (отпуск)
    skip: frozenset = frozenset()                       # отменённые даты «YYYY-MM-DD»

    @property
    def time(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def days_text(self) -> str:
        return ", ".join(WEEKDAYS[d].capitalize() for d in sorted(self.days))


def parse_days(text: str) -> frozenset | None:
    """«пн ср пт» → {0, 2, 4}. None — если ни одного дня не узнали."""
    days = set()
    for w in text.lower().replace(",", " ").replace(".", " ").split():
        for i, (short, full) in enumerate(zip(WEEKDAYS, _DAY_NAMES)):
            if w == short or (len(w) >= 3 and full.startswith(w[:3])):
                days.add(i)
    return frozenset(days) or None


def parse_time(text: str) -> tuple[int, int] | None:
    """«19:00» / «19.30» / «9» → (19, 0)."""
    h, _, m = text.strip().replace(".", ":").partition(":")
    if not h.isdigit() or (m and not m.isdigit()):
        return None
    h, m = int(h), int(m or 0)
    return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None


def live_start(live: Live, day) -> datetime:
    """Начало эфира в этот день (время — по часовому поясу тренера, DEFAULT_TZ)."""
    return datetime(day.year, day.month, day.day, live.hour, live.minute, tzinfo=zone(config.DEFAULT_TZ))


def live_today(live: Live, now: datetime):
    """Дата сегодняшнего эфира (по часовому поясу тренера) или None, если эфира нет."""
    today = now.astimezone(zone(config.DEFAULT_TZ)).date()
    if not live.on or today.weekday() not in live.days or today.isoformat() in live.skip:
        return None
    return today


def live_actions(live: Live, now: datetime, mode: str | None = None,
                 started: bool = False) -> list[tuple[str, str, datetime]]:
    """Что положено участницам тарифа 3x сейчас: [(ключ, вид, начало эфира)].

    mode — решение тренера на сегодня: "rec" — сегодня по записи (запись уходит
    сразу с утреннего часа), иначе — эфир. Вид: day — утром «сегодня эфир»,
    soon — за час «скоро начинаем», rec — тренировка в записи. Если эфир так и не
    начался (started — в группе клуба не открыли видеочат) через LIVE_GRACE_MIN
    после начала, — тоже rec: «сегодня занимаемся по записи»."""
    today = live_today(live, now)
    if today is None:
        return []
    local = now.astimezone(zone(config.DEFAULT_TZ))
    if local.hour >= config.QUIET_HOUR:
        return []
    start = live_start(live, today)
    rec = (f"liverec:{today}", "rec", start)
    if mode == "rec":
        return [rec] if local.hour >= config.LIVE_REMIND_HOUR else []
    out = []
    if start - timedelta(hours=1) <= now < start + timedelta(minutes=20):
        out.append((f"live:{today}:soon", "soon", start))
    elif local.hour >= config.LIVE_REMIND_HOUR and now < start - timedelta(hours=1):
        out.append((f"live:{today}:day", "day", start))
    if not started and now >= start + timedelta(minutes=config.LIVE_GRACE_MIN):
        out.append(rec)
    return out


def next_live(live: Live, now: datetime) -> datetime | None:
    """Ближайший эфир (ещё не закончившийся: до 20 минут после начала)."""
    if not live.on or not live.days:
        return None
    today = now.astimezone(zone(config.DEFAULT_TZ)).date()
    for k in range(15):
        d = today + timedelta(days=k)
        if d.weekday() in live.days and d.isoformat() not in live.skip:
            start = live_start(live, d)
            if now < start + timedelta(minutes=20):
                return start
    return None


# Тренировки в записи — только тариф 6x. Неделя считается от дня покупки (день 0):
CLUB_WEEK = {"6x": {0, 1, 2, 3, 4, 5}}                  # день 6 — отдых


def club_keys(started_at: datetime, tz: str, plan: str, slot: str | None, now: datetime) -> list[str]:
    """Ключи рассылки тренировок в записи, положенные сейчас (для журнала sends).
    В день отдыха после тренировочного — тот же ключ ещё раз: досылка, если
    вчера не ушло (журнал sends не даст прислать дважды)."""
    days = CLUB_WEEK.get(plan)
    local = now.astimezone(zone(tz))
    if not days or local.hour >= config.QUIET_HOUR:
        return []
    i = funnel_day(started_at, now, tz) - 1
    out = []
    for s in slots_of(slot):
        if local.hour < slot_hour(s):
            continue
        if i % 7 in days:
            out.append(f"club:{i}:{s}")
        elif i >= 1 and (i - 1) % 7 in days:
            out.append(f"club:{i - 1}:{s}")
    return out


def club_delivers_on(started_at: datetime, tz: str, plan: str, now: datetime, ahead: int = 0) -> bool:
    i = funnel_day(started_at, now, tz) - 1 + ahead
    return i >= 0 and i % 7 in CLUB_WEEK.get(plan, set())
