"""Тик: будильник раз в час дёргает /tick, бот решает, кому что слать."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone

from aiogram import Bot

from . import config, db, funnel, messages, payments, sender, texts, tribute_router

log = logging.getLogger(__name__)
_lock = asyncio.Lock()


def person(u) -> funnel.Person:
    return funnel.Person(tg_id=u["tg_id"], started_at=u["started_at"], tz=u["tz"],
                         slot=u["slot"], paid=u["paid_at"] is not None,
                         last_done_day=u["last_done_day"])


async def run(bot: Bot, now: datetime | None = None) -> dict:
    if _lock.locked():
        return {"status": "busy"}         # предыдущий тик ещё рассылает
    async with _lock:
        now = now or datetime.now(timezone.utc)
        users = await db.funnel_users()
        planned = [(u, a) for u in users for a in funnel.plan(person(u), now)]
        sent_before = await db.already_sent([(u["tg_id"], a.key) for u, a in planned])
        loaded = await db.workout_days()
        result: Counter = Counter()
        blocked: set[int] = set()
        for u, a in planned:
            if (u["tg_id"], a.key) in sent_before or u["tg_id"] in blocked:
                continue
            if a.kind == "workout" and a.day not in loaded \
                    and now.astimezone(funnel.zone(u["tz"])).hour < config.MISSING_HOUR:
                result["waiting_video"] += 1     # видео дня ещё не загружено — ждём
                continue
            if a.kind in ("sale", "post") and await payments.club_plan_by_membership(bot, u["tg_id"]):
                result["found_in_club"] += 1    # уже в канале клуба — купила, не продаём
                continue
            if not await db.claim_send(u["tg_id"], a.key):
                continue
            status = await messages.send_action(bot, u, a)
            if status == sender.FAILED:
                await db.release_send(u["tg_id"], a.key)   # повторим на следующем тике
            elif status == sender.BLOCKED:
                blocked.add(u["tg_id"])
            result[status] += 1
            result[a.kind] += 1
        live = await messages.live_schedule()
        await run_live(bot, now, live, blocked, result)
        club_today, club_tomorrow = await run_club(bot, now, live.days, blocked, result)
        await alert_missing(bot, users, loaded, now, club_today, club_tomorrow)
        result["tribute_resent"] = await tribute_router.retry_due(bot)
        result["videos_removed"] = await messages.cleanup_media(bot)
        log.info("Тик: участниц %s, отправлено %s", len(users), dict(result))
        return {"status": "done", "users": len(users), **result}


async def run_live(bot: Bot, now: datetime, live: funnel.Live, blocked: set[int], result: Counter) -> None:
    """Напоминания об эфире всем участницам клуба: утром и за час до начала."""
    r = funnel.live_reminder(live, now)
    if r is None:
        return
    key, kind, start = r
    members = await db.club_members_active()
    sent_before = await db.already_sent([(m["tg_id"], key) for m in members])
    link = await messages.live_link()
    for m in members:
        tg = m["tg_id"]
        if (tg, key) in sent_before or tg in blocked or not await db.claim_send(tg, key):
            continue
        status = await messages.send_live_reminder(bot, m, kind, start, link)
        if status == sender.FAILED:
            await db.release_send(tg, key)
        elif status == sender.BLOCKED:
            blocked.add(tg)
        result[f"live_{kind}"] += 1


async def run_club(bot: Bot, now: datetime, live_days, blocked: set[int],
                   result: Counter) -> tuple[Counter, Counter]:
    """Тренировки клуба в записи (тариф 6x): каждой — следующая по номеру, в дни без эфира.
    Вернёт, какие номера клуба (ещё не загруженные) нужны сегодня и завтра."""
    await db.club_backfill(config.DEFAULT_PLAN)
    members = await db.club_members_active()
    loaded = await db.club_workout_numbers()
    planned = [(m, k) for m in members
               for k in funnel.club_keys(m["started_at"], m["tz"], m["plan"], m["slot"], now, live_days)]
    sent_before = await db.already_sent([(m["tg_id"], k) for m, k in planned])
    handled: set[int] = set()
    repeat_now = await db.get_setting("club_repeat") == "on"     # тренер на отдыхе — повторы сразу
    for m, key in planned:
        tg = m["tg_id"]
        if (tg, key) in sent_before or tg in blocked or tg in handled:
            continue
        n = m["next_n"]
        if n not in loaded and not repeat_now \
                and now.astimezone(funnel.zone(m["tz"])).hour < config.MISSING_HOUR:
            result["club_waiting"] += 1          # ждём, пока тренер загрузит №n
            continue
        if not await db.claim_send(tg, key):
            continue
        handled.add(tg)
        if n not in loaded:                      # новой нет — повтор пройденной (номер не двигаем)
            r = messages.next_repeat(list(loaded), n, m["repeat_n"])
            if r is None:
                status = await messages.say(bot, tg, texts.CLUB_MISSING)
            else:
                status = await messages.send_club_repeat(bot, tg, r)
                if status == sender.OK:
                    await db.club_set_repeat(tg, r)
                    result["club_repeat"] += 1
        else:
            status = await messages.send_club_workout(bot, tg, n)
            if status == sender.OK:
                await db.club_advance(tg)
                result["club_workout"] += 1
        if status == sender.FAILED:
            await db.release_send(tg, key)
        elif status == sender.BLOCKED:
            blocked.add(tg)

    # Что загрузить: участницы, у которых сегодня/завтра день тренировки, и их следующий номер
    today: Counter = Counter()
    tomorrow: Counter = Counter()
    if repeat_now:
        return today, tomorrow                   # тренер на отдыхе — не напоминаем
    members = await db.club_members_active()
    today_keys = {m["tg_id"]: [f"club:{funnel.funnel_day(m['started_at'], now, m['tz']) - 1}:{s}"
                               for s in funnel.slots_of(m["slot"])] for m in members}
    sent_today = await db.already_sent([(tg, k) for tg, ks in today_keys.items() for k in ks])
    for m in members:
        pending_today = (funnel.club_delivers_on(m["started_at"], m["tz"], m["plan"], now, live_days=live_days)
                         and not any((m["tg_id"], k) in sent_today for k in today_keys[m["tg_id"]]))
        if pending_today and m["next_n"] not in loaded:
            today[m["next_n"]] += 1
        n_next = m["next_n"] + (1 if pending_today else 0)
        if funnel.club_delivers_on(m["started_at"], m["tz"], m["plan"], now, ahead=1, live_days=live_days) and n_next not in loaded:
            tomorrow[n_next] += 1
    return today, tomorrow


async def alert_missing(bot: Bot, users, loaded: set[int], now: datetime,
                        club_today: Counter | None = None, club_tomorrow: Counter | None = None) -> None:
    """Утром и вечером — напоминание вам, какие тренировки нужно загрузить."""
    today: Counter = Counter()        # номер тренировки → сколько участниц ждут её сегодня
    tomorrow: Counter = Counter()
    for u in users:
        d = funnel.funnel_day(u["started_at"], now, u["tz"])
        if d in funnel.WORKOUT_DAYS:
            today[funnel.WORKOUT_DAYS[d]] += 1
        if d + 1 in funnel.WORKOUT_DAYS:
            tomorrow[funnel.WORKOUT_DAYS[d + 1]] += 1
    need_today = sorted(d for d in today if d not in loaded)
    need_tomorrow = sorted(d for d in tomorrow if d not in loaded and d not in need_today)

    local = now.astimezone(funnel.zone(config.DEFAULT_TZ))
    if local.hour >= config.QUIET_HOUR:
        return
    if local.hour >= config.ADMIN_EVENING_HOUR:
        part = "pm"
    elif local.hour >= config.ADMIN_MORNING_HOUR:
        part = "am"
    else:
        return
    club_today, club_tomorrow = club_today or Counter(), club_tomorrow or Counter()
    lines = ([f"• тренировка {d} — сегодня ({today[d]} уч.)" for d in need_today]
             + [f"• тренировка {d} — завтра ({tomorrow[d]} уч.)" for d in need_tomorrow]
             + [f"• клуб №{n} — сегодня ({club_today[n]} уч.) → /upload_club {n}" for n in sorted(club_today)]
             + [f"• клуб №{n} — завтра ({club_tomorrow[n]} уч.) → /upload_club {n}"
                for n in sorted(club_tomorrow) if n not in club_today])
    if not lines or not await db.claim_alert(f"missing:{local.date()}:{part}"):
        return
    await messages.notify_admins(
        bot, "🎬 Нужно загрузить тренировки:\n" + "\n".join(lines)
        + f"\n\nПока видео нет, участницы ждут его до {config.MISSING_HOUR}:00, "
          "потом получат мягкое сообщение «день отдыха».\nЗагрузка: /upload 2")
