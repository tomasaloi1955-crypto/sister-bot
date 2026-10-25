"""Сборка и отправка сообщений воронки (общие для тика и обработчиков)."""
from __future__ import annotations

import html
import logging
import random

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from . import config, db, funnel, keyboards, sender, texts

log = logging.getLogger(__name__)
CAPTION_LIMIT = 1024


def name_of(u) -> str:
    return html.escape((u["first_name"] if u else None) or "сестра")


async def say(bot: Bot, tg_id: int, text: str,
              markup: InlineKeyboardMarkup | None = None) -> str:
    return await sender.call(tg_id, lambda: bot.send_message(
        tg_id, text, reply_markup=markup, disable_web_page_preview=True))


async def notify_admins(bot: Bot, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    for admin in config.ADMIN_IDS:
        try:
            await bot.send_message(admin, text, reply_markup=markup)
        except Exception:
            log.exception("Не удалось уведомить админа %s", admin)


async def send_workout(bot: Bot, tg_id: int, day: int, slot: str) -> str:
    w = await db.get_workout(day, slot)
    if w is None:
        log.warning("Нет тренировки на день %s — отправляем заглушку", day)
        return await say(bot, tg_id, texts.WORKOUT_MISSING.format(day=day, total=funnel.WORKOUTS), keyboards.done(day))
    header = texts.WORKOUT_HEADER.format(day=day, total=funnel.WORKOUTS)
    body = header + (w["text"] or "")
    video = w["video"] or ""
    kb = keyboards.done(day)
    if not video:
        return await say(bot, tg_id, body, kb)
    if video.startswith("http"):
        return await say(bot, tg_id, body + texts.WORKOUT_LINK.format(link=html.escape(video)), kb)

    is_doc = video.startswith("doc:")
    file_id = video[4:] if is_doc else video
    method = bot.send_document if is_doc else bot.send_video
    if len(body) <= CAPTION_LIMIT:
        return await sender.call(tg_id, lambda: method(tg_id, file_id, caption=body, reply_markup=kb))
    # Длинный текст в подпись не влезает: видео отдельно, текст с кнопкой — следом.
    result = await sender.call(tg_id, lambda: method(tg_id, file_id, caption=header))
    if result != sender.OK:
        return result
    return await say(bot, tg_id, body, kb)


def plan_prices() -> dict:
    return {f"price_{code}": plan["price"] for code, plan in config.PLANS.items()}


async def send_club_offer(bot: Bot, u) -> str:
    """Карточка клуба: два тарифа и кнопки оплаты."""
    return await say(bot, u["tg_id"], texts.CLUB_OFFER.format(**plan_prices()), keyboards.pay())


async def send_action(bot: Bot, u, a: funnel.Action) -> str:
    """Отправить одно плановое сообщение из funnel.plan()."""
    tg_id, name = u["tg_id"], name_of(u)
    prices = plan_prices()

    if a.kind == "workout":
        return await send_workout(bot, tg_id, a.day, a.slot)
    if a.kind == "checkin":
        return await say(bot, tg_id, texts.CHECKIN.format(name=name, week=a.day // 7),
                         keyboards.checkin(a.day))
    if a.kind == "nudge":
        return await say(bot, tg_id, texts.NUDGE.format(name=name))
    if a.kind == "warm_stats":
        done = await db.done_count(tg_id)
        days = funnel.workouts_received(a.day)
        if done == 0:
            return await say(bot, tg_id, texts.WARM_STATS_ZERO.format(name=name))
        return await say(bot, tg_id, texts.WARM_STATS.format(
            name=name, done=done, days=days, minutes=done * texts.MINUTES_PER_WORKOUT))
    if a.kind == "warm_reviews":
        rows = await db.approved_reviews(tg_id)
        if not rows:
            return await say(bot, tg_id, texts.WARM_REVIEWS_EMPTY)
        reviews = "\n\n".join(texts.REVIEW_ITEM.format(
            text=html.escape(r["text"]), name=html.escape(r["first_name"] or "сестра")) for r in rows)
        return await say(bot, tg_id, texts.WARM_REVIEWS.format(reviews=reviews))
    if a.kind == "sale":
        text = (texts.SALE_OFFER, texts.SALE_OBJECTIONS, texts.SALE_LAST)[funnel.SALE_DAYS.index(a.day)]
        return await say(bot, tg_id, text.format(name=name, **prices), keyboards.club())
    if a.kind == "post":
        text = (texts.POST[funnel.POST_DAYS.index(a.day)] if a.day in funnel.POST_DAYS
                else random.choice(texts.PERIODIC))
        return await say(bot, tg_id, text.format(name=name, **prices),
                         keyboards.club(with_unsubscribe=True))
    log.error("Неизвестный тип сообщения: %s", a.kind)
    return sender.FAILED
