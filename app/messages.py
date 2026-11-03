"""Сборка и отправка сообщений воронки (общие для тика и обработчиков)."""
from __future__ import annotations

import html
import logging
import random
import struct
import zlib
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InputMediaPhoto

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
    w = await db.get_workout(day, slot)
    if w is None:
        log.warning("Нет тренировки на день %s — отправляем заглушку", day)
        return await say(bot, tg_id, texts.WORKOUT_MISSING.format(day=day, total=funnel.WORKOUTS), keyboards.done(day))
    header = texts.WORKOUT_HEADER.format(day=day, total=funnel.WORKOUTS)
    return await _deliver(bot, tg_id, w, header, keyboards.done(day), track, day, False, is_rewatch)


async def send_club_workout(bot: Bot, tg_id: int, n: int, track: bool = True, is_rewatch: bool = False) -> str:
    """Тренировка клуба №n — так же защищена и удаляется из чата."""
    w = await db.get_club_workout(n)
    if w is None:
        return await say(bot, tg_id, texts.CLUB_MISSING)
    return await _deliver(bot, tg_id, w, texts.CLUB_HEADER.format(n=n), keyboards.club_done(n),
                          track, n, True, is_rewatch)


async def send_club_repeat(bot: Bot, tg_id: int, n: int) -> str:
    """Повтор прошлой тренировки клуба №n с усложнением (новой ещё нет)."""
    w = await db.get_club_workout(n)
    if w is None:
        return await say(bot, tg_id, texts.CLUB_MISSING)
    return await _deliver(bot, tg_id, w, texts.CLUB_REPEAT_HEADER.format(n=n), keyboards.club_done(n),
                          True, n, True, False)


def next_repeat(loaded: list[int], next_n: int, last_repeat: int) -> int | None:
    """Какую прошлую тренировку повторить: следующую по кругу после last_repeat
    среди уже пройденных (номера меньше next_n). None — повторять нечего."""
    past = sorted(x for x in loaded if x < next_n)
    if not past:
        return None
    later = [x for x in past if x > last_repeat]
    return later[0] if later else past[0]




async def _deliver(bot: Bot, tg_id: int, w, header: str, kb, track: bool, n: int, club: bool,
                   is_rewatch: bool) -> str:
    ids: list[int] = []
    # Любое видео (и бесплатной недели, и клуба) исчезает из чата через MEDIA_TTL_HOURS.
    status = await _send_workout(bot, tg_id, w, header, kb, ids, expires=track)
    if track and status == sender.OK and ids:
        await db.media_add(tg_id, n, ids, is_rewatch, config.MEDIA_TTL_HOURS, club)
    return status


async def _send_workout(bot: Bot, tg_id: int, w, header: str, kb, ids: list[int], expires: bool) -> str:
    async def kept(coro):
        msg = await coro
        ids.append(msg.message_id)

    video = w["video"] or ""
    if video:
        header += texts.VIDEO_AMANAT             # «видео — аманат» — над каждым видео
    body = header + (w["text"] or "")
    if expires and video:
        body += texts.WORKOUT_EXPIRES
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


def _solid_png(width: int = 640, height: int = 360, rgb: tuple = (60, 52, 72)) -> bytes:
    """Однотонная картинка-заглушка вместо удалённого видео (без сторонних библиотек)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    row = b"\x00" + bytes(rgb) * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height, 9)) + chunk(b"IEND", b""))


PLACEHOLDER_PNG = _solid_png()


async def _remove_from_chat(bot: Bot, tg_id: int, mid: int) -> bool:
    """Убрать видео из чата. Удалить бот может только первые 48 часов — позже
    заменяем само сообщение заглушкой (редактировать своё сообщение можно и потом).
    True — видео в чате больше нет."""
    try:
        await bot.delete_message(tg_id, mid)
        return True
    except Exception as e:
        if "not found" in str(e).lower():          # сама уже удалила
            return True
        log.info("Видео %s у %s не удалить (%s) — заменяем заглушкой", mid, tg_id, e)
    try:
        await bot.edit_message_media(
            chat_id=tg_id, message_id=mid,
            media=InputMediaPhoto(media=BufferedInputFile(PLACEHOLDER_PNG, "deleted.png"),
                                  caption=texts.VIDEO_REPLACED))
        return True
    except Exception as e:
        log.info("Заменить сообщение %s у %s как медиа не вышло: %s", mid, tg_id, e)
    try:                                           # текстовая часть тренировки или ссылка
        await bot.edit_message_text(texts.VIDEO_REPLACED, chat_id=tg_id, message_id=mid)
        return True
    except Exception as e:
        if "not found" in str(e).lower():
            return True
        log.warning("Сообщение %s у %s не удалось ни удалить, ни заменить: %s", mid, tg_id, e)
    return False


async def _gone_notice(bot: Bot, tg_id: int, row) -> None:
    """«Видео удалено» + «Посмотреть снова» (если повтора ещё не было)."""
    n, club = row["workout"], row["club"]
    _, rewatched = await db.media_rewatch_state(tg_id, n, club)
    label = f"клуба №{n}" if club else str(n)
    if rewatched:
        await say(bot, tg_id, texts.VIDEO_GONE_FINAL.format(n=label, coach=config.COACH_NAME))
    else:
        await say(bot, tg_id, texts.VIDEO_GONE.format(n=label), keyboards.rewatch(n, club))


async def purge_media(bot: Bot, tg_id: int, club_only: bool = False) -> int:
    """Убрать у сестры из чата отправленные видео тренировок сейчас (и те, что раньше
    не удалось удалить). club_only — только клуб: подписка кончилась, без «посмотреть
    снова». Вернёт, сколько тренировок убрано."""
    done = 0
    for row in await db.media_of(tg_id):
        if club_only and not row["club"]:
            continue
        removed = [await _remove_from_chat(bot, tg_id, mid) for mid in row["message_ids"]]
        if club_only:
            if row["deleted_at"] is None:
                await db.media_mark_deleted(row["id"])
        elif row["deleted_at"] is None:
            await db.media_mark_deleted(row["id"])
            if any(removed):
                await _gone_notice(bot, tg_id, row)
        done += any(removed)
    return done


async def cleanup_media(bot: Bot) -> int:
    """Из тика: убрать из чатов видео, у которых вышел срок. Вернёт, сколько убрано."""
    done = 0
    # Видео клуба, отправленные «навсегда»: свежие получают обычный срок, а старые
    # убираем молча — без пачки «видео удалено» за каждую прошлую тренировку.
    for row in await db.media_club_kept():
        if row["created_at"] > datetime.now(timezone.utc) - timedelta(hours=config.MEDIA_TTL_HOURS):
            await db.media_set_delete_after(row["id"], config.MEDIA_TTL_HOURS)
            continue
        removed = [await _remove_from_chat(bot, row["tg_id"], mid) for mid in row["message_ids"]]
        await db.media_mark_deleted(row["id"])
        done += any(removed)
    for row in await db.media_due():
        removed = [await _remove_from_chat(bot, row["tg_id"], mid) for mid in row["message_ids"]]
        await db.media_mark_deleted(row["id"])
        if not any(removed):
            continue                               # не обещаем «удалено», если видео осталось
        done += 1
        await _gone_notice(bot, row["tg_id"], row)
    return done


def plan_prices() -> dict:
    return {f"price_{code}": plan["price"] for code, plan in config.PLANS.items()}


# ─────────────────────────── живые эфиры клуба ───────────────────────────

async def live_schedule() -> funnel.Live:
    """Расписание эфиров: из /live (таблица settings), иначе из переменных LIVE_*."""
    days = funnel.parse_days(await db.get_setting("live_days") or config.LIVE_DAYS) or funnel.Live().days
    hour, minute = funnel.parse_time(await db.get_setting("live_time") or config.LIVE_TIME) or (19, 0)
    return funnel.Live(days=days, hour=hour, minute=minute,
                       on=(await db.get_setting("live_on") or "on") == "on",
                       skip=frozenset((await db.get_setting("live_skip") or "").split()))


async def live_link() -> str:
    link = await db.get_setting("live_link")
    return config.LIVE_LINK if link is None else link


def live_text(live: funnel.Live) -> str:
    """«Пн, Ср, Пт в 19:00 (по Алматы)» — для текстов продажи и приветствия."""
    return texts.LIVE_WHEN.format(days=live.days_text, time=live.time)


def local_time(start, tz: str | None) -> str:
    return start.astimezone(funnel.zone(tz or config.DEFAULT_TZ)).strftime("%H:%M")


async def offer_vars() -> dict:
    return {**plan_prices(), "live": live_text(await live_schedule())}


async def send_live_reminder(bot: Bot, m, kind: str, start, link: str) -> str:
    text = texts.LIVE_SOON if kind == "soon" else texts.LIVE_TODAY
    return await say(bot, m["tg_id"], text.format(name=name_of(m), time=local_time(start, m["tz"])),
                     keyboards.live(link))


async def send_club_offer(bot: Bot, u) -> str:
    """Карточка клуба: два тарифа и кнопки оплаты."""
    return await say(bot, u["tg_id"], texts.CLUB_OFFER.format(**await offer_vars()), keyboards.pay())


async def send_action(bot: Bot, u, a: funnel.Action) -> str:
    """Отправить одно плановое сообщение из funnel.plan()."""
    tg_id, name = u["tg_id"], name_of(u)
    prices = await offer_vars() if a.kind in ("sale", "post") else {}

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
