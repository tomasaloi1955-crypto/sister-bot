"""Тик: будильник раз в час дёргает /tick, бот решает, кому что слать."""
from __future__ import annotations

import asyncio
import html
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import config, db, funnel, keyboards, messages, payments, sender, texts, tribute_router

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
        club_today, club_tomorrow = await run_club(bot, now, blocked, result)
        await alert_missing(bot, users, loaded, now, club_today, club_tomorrow)
        await alert_club_clicks(bot, now)
        await alert_warm(bot, users, now)
        result["tribute_resent"] = await tribute_router.retry_due(bot)
        result["videos_removed"] = await messages.cleanup_media(bot)
        log.info("Тик: участниц %s, отправлено %s", len(users), dict(result))
        return {"status": "done", "users": len(users), **result}


async def run_live(bot: Bot, now: datetime, live: funnel.Live, blocked: set[int], result: Counter) -> None:
    """День эфира (/live). «5 раз в неделю» — эфир: напоминания утром и за час; эфир
    отменён (тренер, /live skip, отпуск) или так и не начался — тренировка в записи лично.
    «3 раза в неделю» — тренировка в записи, а эфир — только если тренер позвала и их."""
    today = funnel.live_today(live, now)
    if today is None:
        return
    mode = await db.get_setting(f"live_mode:{today}")
    cancelled = funnel.live_cancelled(live, today, mode)
    invite3 = await db.get_setting(f"live_all:{today}") == "1"
    started = bool(await db.get_setting(f"live_started:{today}"))
    if not cancelled:
        await ask_coach(bot, live, today, mode, now)
    members = await db.club_members_active()
    five = [m for m in members if m["plan"] == "6x"]
    three = [m for m in members if m["plan"] != "6x"]
    rec_only = funnel.live_actions(live, now, "rec")
    on_air = funnel.live_actions(live, now, "live", started)
    reminders = [a for a in on_air if a[1] != "rec"]
    # (кто, что положено, текст к записи)
    groups = ((five, rec_only, texts.LIVE_REC_CANCELLED) if cancelled else (five, on_air, texts.LIVE_REC_MISSED),
              (three, rec_only + (reminders if invite3 and not cancelled else []), texts.LIVE_REC_PLANNED))
    loaded = await db.club_workout_numbers()
    free = await db.workout_days()
    link = await messages.live_link()
    for group, actions, rec_text in groups:
        for key, kind, start in actions:
            sent_before = await db.already_sent([(m["tg_id"], key) for m in group])
            todo = [m for m in group if (m["tg_id"], key) not in sent_before and m["tg_id"] not in blocked]
            if kind == "rec" and rec_text is texts.LIVE_REC_MISSED and todo \
                    and await db.claim_alert(f"livemissed:{today}"):
                await messages.notify_admins(bot, f"🎬 Эфир {start:%d.%m} в {start:%H:%M} не начался (видеочата в "
                                                  f"группе нет) — шлю сёстрам тренировку в записи.")
            for m in todo:
                tg = m["tg_id"]
                if not await db.claim_send(tg, key):
                    continue
                if kind == "rec":
                    status = await messages.say(bot, tg, rec_text.format(name=messages.name_of(m)))
                    if status == sender.OK:
                        status = await deliver_next(bot, m, loaded, result, free)
                else:
                    status = await messages.send_live_reminder(bot, m, kind, start, link)
                if status == sender.FAILED:
                    await db.release_send(tg, key)
                elif status == sender.BLOCKED:
                    blocked.add(tg)
                result[f"live_{kind}"] += 1


async def ask_coach(bot: Bot, live: funnel.Live, today, mode: str | None, now: datetime) -> None:
    """Утром в день эфира — спросить тренера: эфир будет? Позвать и «3 раза в неделю»?"""
    start = funnel.live_start(live, today)
    local = now.astimezone(funnel.zone(config.DEFAULT_TZ))
    if mode or local.hour < config.ADMIN_MORNING_HOUR or now >= start - timedelta(hours=1):
        return
    if not await db.claim_alert(f"livechoice:{today}"):
        return
    await messages.notify_admins(
        bot, texts.LIVE_ASK_COACH.format(time=f"{start:%H:%M}", grace=config.LIVE_GRACE_MIN),
        keyboards.live_choice(today.isoformat()))


async def deliver_next(bot: Bot, m, loaded: dict, result: Counter, free: set[int]) -> str:
    """Следующая тренировка в записи по её номеру; нет новой — повтор по кругу:
    тренировки бесплатной недели и пройденные клуба."""
    tg, n = m["tg_id"], m["next_n"]
    if n in loaded:
        status = await messages.send_club_workout(bot, tg, n)
        if status == sender.OK:
            await db.club_advance(tg)
            result["club_workout"] += 1
        return status
    r = messages.next_repeat(list(loaded), n, m["repeat_n"], free)      # номер не двигаем
    if r is None:
        return await messages.say(bot, tg, texts.CLUB_MISSING)
    status = await messages.send_club_repeat(bot, tg, r)
    if status == sender.OK:
        await db.club_set_repeat(tg, r)
        result["club_repeat"] += 1
    return status


async def run_club(bot: Bot, now: datetime, blocked: set[int],
                   result: Counter) -> tuple[Counter, Counter]:
    """Тренировки в записи (тариф 6x) в дни без эфира (вт, сб): каждой — следующая
    по номеру. Вернёт, какие номера клуба (ещё не загруженные) нужны сегодня и завтра."""
    await db.club_backfill(config.DEFAULT_PLAN)
    members = await db.club_members_active()
    loaded = await db.club_workout_numbers()
    free = await db.workout_days()
    live_days = (await messages.live_schedule()).days
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
        status = await deliver_next(bot, m, loaded, result, free)
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


def _who(u) -> str:
    return (f'<a href="tg://user?id={u["tg_id"]}">{messages.name_of(u)}</a>'
            + (f" @{html.escape(u['username'])}" if u["username"] else "") + f" (id {u['tg_id']})")


async def alert_club_clicks(bot: Bot, now: datetime) -> None:
    """Открыла тарифы клуба больше часа назад и не купила — тренеру, чтобы написала лично."""
    local = now.astimezone(funnel.zone(config.DEFAULT_TZ))
    if not (config.ADMIN_MORNING_HOUR <= local.hour < config.QUIET_HOUR):
        return
    for u in await db.club_clicks_unpaid(now - timedelta(days=2), now - timedelta(hours=1)):
        if await db.claim_alert(f"clubclick:{u['tg_id']}"):
            await messages.notify_admins(
                bot, f"💎 {_who(u)} открыла тарифы клуба, но пока не оплатила.\n"
                     "Самое время написать ей лично: может, есть вопрос или сомнение.",
                keyboards.reply_to(u["tg_id"]))


def warm_lines(users, info: dict, now: datetime) -> list[tuple[int, str, object]]:
    """Сёстры, которым сегодня пришло предложение клуба (день 7): самые тёплые — сверху."""
    rows = []
    for u in users:
        if u["paid_at"] is not None or funnel.funnel_day(u["started_at"], now, u["tz"]) != funnel.SALE_DAYS[0]:
            continue
        w = info.get(u["tg_id"])
        done, mood, clicked = (w["done"], w["mood"], w["clicked"]) if w else (0, None, False)
        score = done + (2 if clicked else 0) + (1 if mood else 0)
        marks = [f"сделала {done} из {funnel.WORKOUTS}"]
        if mood:
            marks.append(texts.MOODS.get(mood, mood))
        if clicked:
            marks.append("💎 смотрела тарифы")
        line = ("🔥 " if score >= 2 else "• ") + _who(u) + " — " + ", ".join(marks)
        if w and w["review"]:
            line += f"\n   «{html.escape(w['review'][:150])}»"
        rows.append((score, line, u))
    return sorted(rows, key=lambda r: -r[0])


async def alert_warm(bot: Bot, users, now: datetime) -> None:
    """Вечером — список сестёр, получивших сегодня предложение клуба, для личного сообщения."""
    local = now.astimezone(funnel.zone(config.DEFAULT_TZ))
    if not (config.ADMIN_EVENING_HOUR <= local.hour < config.QUIET_HOUR):
        return
    candidates = [u for u in users if u["paid_at"] is None]
    info = await db.warmth([u["tg_id"] for u in candidates])
    rows = warm_lines(candidates, info, now)
    if not rows or not await db.claim_alert(f"warm:{local.date()}"):
        return
    hot = [(u["tg_id"], messages.name_of(u)) for score, _, u in rows if score >= 2][:10]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"✍️ {html.unescape(name)}", callback_data=f"adm:reply:{tg}")] for tg, name in hot]) if hot else None
    await messages.notify_admins(
        bot, "🤍 <b>Кому написать лично</b> — сегодня им пришло предложение клуба.\n"
             "🔥 — самые тёплые: занимались, отвечали или смотрели тарифы. "
             "Ответить через бота — кнопки ниже, или нажмите на имя.\n\n"
             + "\n".join(line for _, line, _ in rows[:40])
             + "\n\n🎁 Подарок (личный план первым 10) они увидят на 9-й день — можно сказать о нём уже сейчас.",
        kb)
