"""Тик: GitHub Actions раз в 15 минут дёргает /tick, бот решает, кому что слать."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone

from aiogram import Bot

from . import config, db, funnel, messages, sender

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
            if not await db.claim_send(u["tg_id"], a.key):
                continue
            status = await messages.send_action(bot, u, a)
            if status == sender.FAILED:
                await db.release_send(u["tg_id"], a.key)   # повторим на следующем тике
            elif status == sender.BLOCKED:
                blocked.add(u["tg_id"])
            result[status] += 1
            result[a.kind] += 1
        await alert_missing(bot, users, loaded, now)
        log.info("Тик: участниц %s, отправлено %s", len(users), dict(result))
        return {"status": "done", "users": len(users), **result}


async def alert_missing(bot: Bot, users, loaded: set[int], now: datetime) -> None:
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
    lines = ([f"• тренировка {d} — сегодня ({today[d]} уч.)" for d in need_today]
             + [f"• тренировка {d} — завтра ({tomorrow[d]} уч.)" for d in need_tomorrow])
    if not lines or not await db.claim_alert(f"missing:{local.date()}:{part}"):
        return
    await messages.notify_admins(
        bot, "🎬 Нужно загрузить тренировки:\n" + "\n".join(lines)
        + f"\n\nПока видео нет, участницы ждут его до {config.MISSING_HOUR}:00, "
          "потом получат мягкое сообщение «день отдыха».\nЗагрузка: /upload 2")
