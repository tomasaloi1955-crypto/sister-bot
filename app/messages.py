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
              markup: InlineKeyboardMarkup | None = None, protect: bool = False) -> str:
    return await sender.call(tg_id, lambda: bot.send_message(
        tg_id, text, reply_markup=markup, disable_web_page_preview=True, protect_content=protect))


async def notify_admins(bot: Bot, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    for admin in config.ADMIN_IDS:
        try:
            await bot.send_message(admin, text, reply_markup=markup)
        except Exception:
            log.exception("Не удалось уведомить админа %s", admin)


async def send_workout(bot: Bot, tg_id: int, day: int, slot: str, track: bool = True,
                       is_rewatch: bool = False) -> str:
    """Тренировка с защитой Telegram (protect_content): её нельзя переслать,
    сохранить или скопировать. Видео-ссылку (http…) защитить нельзя — поэтому
    видео лучше загружать файлом.

    track — участнице (не админу-предпросмотру): видео удаляется из чата через
    MEDIA_TTL_HOURS, вместо него остаётся «🔁 Посмотреть снова» (один раз)."""
    ids: list[int] = []
    status = await _send_workout(bot, tg_id, day, slot, ids, track)
    if track and status == sender.OK and ids:
        await db.media_add(tg_id, day, ids, is_rewatch, config.MEDIA_TTL_HOURS)
    return status


async def _send_workout(bot: Bot, tg_id: int, day: int, slot: str, ids: list[int], track: bool) -> str:
    async def kept(coro):
        msg = await coro
        ids.append(msg.message_id)

    w = await db.get_workout(day, slot)
    if w is None:
        log.warning("Нет тренировки на день %s — отправляем заглушку", day)
        return await say(bot, tg_id, texts.WORKOUT_MISSING.format(day=day, total=funnel.WORKOUTS), keyboards.done(day))
    header = texts.WORKOUT_HEADER.format(day=day, total=funnel.WORKOUTS)
    body = header + (w["text"] or "")
    video = w["video"] or ""
    if track and video:
        body += texts.WORKOUT_EXPIRES
    kb = keyboards.done(day)
    if not video:
        return await say(bot, tg_id, body, kb, protect=True)
    if video.startswith("http"):
        return await sender.call(tg_id, lambda: kept(bot.send_message(
            tg_id, body + texts.WORKOUT_LINK.format(link=html.escape(video)), reply_markup=kb,
            disable_web_page_preview=True, protect_content=True)))

    is_doc = video.startswith("doc:")
    file_id = video[4:] if is_doc else video
    method = bot.send_document if is_doc else bot.send_video
    if len(body) <= CAPTION_LIMIT:
        return await sender.call(tg_id, lambda: kept(method(tg_id, file_id, caption=body, reply_markup=kb,
                                                            protect_content=True)))
    # Длинный текст в подпись не влезает: видео отдельно, текст с кнопкой — следом.
    result = await sender.call(tg_id, lambda: kept(method(tg_id, file_id, caption=header, protect_content=True)))
    if result != sender.OK:
        return result
    return await sender.call(tg_id, lambda: kept(bot.send_message(
        tg_id, body, reply_markup=kb, disable_web_page_preview=True, protect_content=True)))


async def cleanup_media(bot: Bot) -> int:
    """Из тика: удалить из чатов видео, у которых вышел срок. Вернёт, сколько удалено."""
    done = 0
    for row in await db.media_due():
        for mid in row["message_ids"]:
            try:
                await bot.delete_message(row["tg_id"], mid)
            except Exception as e:                 # уже удалено самой, или прошло 48 часов
                log.info("Видео %s у %s не удалено: %s", mid, row["tg_id"], e)
        await db.media_mark_deleted(row["id"])
        done += 1
        _, rewatched = await db.media_rewatch_state(row["tg_id"], row["workout"])
        n = row["workout"]
        if rewatched:
            await say(bot, row["tg_id"], texts.VIDEO_GONE_FINAL.format(n=n, coach=config.COACH_NAME))
        else:
            await say(bot, row["tg_id"], texts.VIDEO_GONE.format(n=n), keyboards.rewatch(n))
    return done


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
