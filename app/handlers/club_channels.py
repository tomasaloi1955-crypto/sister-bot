"""Каналы и группы клуба: вступила — значит, купила (впускает туда Tribute).

Бота добавляют администратором в канал → он спрашивает админа кнопками, чей это
канал (клуб 3 раза / клуб каждый день / не клуб). Дальше, когда в канал клуба
вступает человек, бот отмечает покупку, и продающие сообщения ей прекращаются.
Запасная проверка — payments.club_plan_by_membership() перед продажей в тике.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup

from .. import config, db, keyboards, messages, payments, sender, texts

log = logging.getLogger(__name__)
router = Router(name="club_channels")
admin_only = F.from_user.id.in_(config.ADMIN_IDS)

PLAN_LABELS = {"3x": "🌿 Клуб «3 раза в неделю»", "6x": "🔥 Клуб «5 раз в неделю»", "none": "📣 Не клуб"}
IN_CHANNEL = {"member", "restricted"}        # админы и создатель — не покупательницы


def _kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[B(text=label, callback_data=f"adm:ch:{code}:{chat_id}")]
                                                 for code, label in PLAN_LABELS.items()])


CLUB_CHAT_TYPES = {"channel", "group", "supergroup"}


@router.my_chat_member(F.chat.type.in_(CLUB_CHAT_TYPES))
async def bot_added(update: ChatMemberUpdated, bot: Bot) -> None:
    """Бота сделали администратором канала — спросить, чей канал."""
    if update.new_chat_member.status != "administrator":
        return
    known = {r["chat_id"]: r["plan"] for r in await db.club_channels()}
    await db.remember_channel(update.chat.id, update.chat.title)
    if known.get(update.chat.id):
        return
    await messages.notify_admins(
        bot, f"📢 Бот стал администратором {'канала' if update.chat.type == 'channel' else 'группы'} "
             f"«{html.escape(update.chat.title or '')}». Это клуб?",
        _kb(update.chat.id))


@router.message(F.chat.type == "private", admin_only, StateFilter(None), F.forward_origin.chat.type == "channel")
async def forwarded_from_channel(message: Message, bot: Bot) -> None:
    """Запасной путь: админ переслал боту пост из канала — спросить, чей канал."""
    chat = message.forward_origin.chat
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
        is_admin = me.status == "administrator"
    except Exception:
        is_admin = False
    if not is_admin:
        await message.answer(f"Я не администратор канала «{html.escape(chat.title or '')}». "
                             "Добавьте меня туда администратором и перешлите пост ещё раз.")
        return
    await db.remember_channel(chat.id, chat.title)
    await message.answer(f"📢 Канал «{html.escape(chat.title or '')}». Что это за канал?", reply_markup=_kb(chat.id))


@router.message(F.chat.type.in_({"group", "supergroup"}), admin_only, Command("clubhere"))
async def club_here(message: Message, bot: Bot) -> None:
    """Админ пишет /clubhere в группе — бот спрашивает в личке, чья это группа
    (переслать боту «пост из группы» нельзя — это путь только для каналов)."""
    chat = message.chat
    try:
        is_admin = (await bot.get_chat_member(chat.id, bot.id)).status == "administrator"
    except Exception:
        is_admin = False
    if is_admin:
        await db.remember_channel(chat.id, chat.title)
        text = (f"📢 Группа «{html.escape(chat.title or '')}». Что это за группа?")
        markup = _kb(chat.id)
    else:
        text = (f"Я не администратор группы «{html.escape(chat.title or '')}». "
                "Сделайте меня администратором и напишите там /clubhere ещё раз.")
        markup = None
    try:
        await bot.send_message(message.from_user.id, text, reply_markup=markup)
    except Exception:
        log.exception("Не удалось написать админу %s в личку", message.from_user.id)
    try:
        await message.delete()                    # чтобы команда не висела в группе
    except Exception:
        pass


@router.message(F.migrate_to_chat_id)
async def migrated(message: Message) -> None:
    """Группа стала супергруппой — у неё новый id, тариф переносим туда."""
    await db.move_channel(message.chat.id, message.migrate_to_chat_id)
    log.info("Группа «%s» стала супергруппой: %s → %s", message.chat.title, message.chat.id,
             message.migrate_to_chat_id)


@router.message(F.video_chat_started)
@router.channel_post(F.video_chat_started)
async def video_chat_started(message: Message) -> None:
    """В канале/группе клуба начался видеочат — эфир идёт, запись вместо него не шлём."""
    plans = {r["chat_id"]: r["plan"] for r in await db.club_channels()}
    if plans.get(message.chat.id) not in config.PLANS and any(p in config.PLANS for p in plans.values()):
        return                                   # видеочат не в клубе
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(config.DEFAULT_TZ)).date()
    await db.set_setting(f"live_started:{today}", "1")
    log.info("Эфир начался в «%s»", message.chat.title)


@router.message(F.chat.type == "private", admin_only, Command("channels"))
async def channels_list(message: Message) -> None:
    """Все каналы и группы, где бот — администратор, и какой тариф им назначен."""
    rows = await db.club_channels()
    if not rows:
        await message.answer("Бот пока не знает ни одного канала или группы. Сделайте его администратором "
                             "и напишите в группе /clubhere (в канале — перешлите боту любой пост).")
        return
    await message.answer("Каналы и группы бота. Нажмите, чтобы поменять:")
    for r in rows:
        await message.answer(f"«{html.escape(r['title'] or str(r['chat_id']))}» — сейчас: "
                             f"{PLAN_LABELS.get(r['plan'] or '', '❓ не выбрано')}", reply_markup=_kb(r["chat_id"]))


@router.callback_query(admin_only, F.data.startswith("adm:ch:"))
async def channel_plan(cb: CallbackQuery) -> None:
    _, _, code, chat_id = cb.data.split(":", 3)
    if code not in PLAN_LABELS:
        return await cb.answer()
    await db.set_channel_plan(int(chat_id), code)
    await cb.answer("Запомнила")
    await cb.message.edit_text(cb.message.html_text + f"\n\n→ {PLAN_LABELS[code]}")


async def hello_in_group(bot: Bot, chat_id: int, user) -> None:
    """Бот не может написать ей первым — зовём из группы кнопкой «Получать тренировки».
    Сообщение удалим, когда она нажмёт «Начать» (см. user.start)."""
    mention = f'<a href="tg://user?id={user.id}">{html.escape(user.first_name or "сестра")}</a>'
    try:
        msg = await bot.send_message(chat_id, texts.CLUB_GROUP_HELLO.format(mention=mention),
                                     reply_markup=keyboards.open_bot((await bot.me()).username))
        await db.set_setting(f"grouphello:{user.id}", f"{chat_id}:{msg.message_id}")
    except Exception:
        log.exception("Не удалось написать приветствие в группе %s", chat_id)


async def forget_group_hello(bot: Bot, tg_id: int) -> None:
    """Нажала «Начать» — приглашение в группе больше не нужно, убираем его."""
    value = await db.get_setting(f"grouphello:{tg_id}")
    if not value:
        return
    await db.del_setting(f"grouphello:{tg_id}")
    chat_id, _, msg_id = value.partition(":")
    try:
        await bot.delete_message(int(chat_id), int(msg_id))
    except Exception:
        pass


@router.message(F.chat.type == "private", admin_only, Command("clubinvite"))
async def club_invite(message: Message, bot: Bot) -> None:
    """Закрепить в каждой группе клуба кнопку «Получать тренировки» — для тех,
    кто уже в группе, но бота ни разу не открывал."""
    username = (await bot.me()).username
    done = []
    for ch in await db.club_channels():
        if ch["plan"] not in config.PLANS:
            continue
        title = html.escape(ch["title"] or str(ch["chat_id"]))
        try:
            msg = await bot.send_message(ch["chat_id"], texts.CLUB_GROUP_PIN, reply_markup=keyboards.open_bot(username))
        except Exception as e:
            done.append(f"⚠️ «{title}»: не смогла написать ({html.escape(str(e))[:150]})")
            continue
        try:
            await bot.pin_chat_message(ch["chat_id"], msg.message_id, disable_notification=True)
            done.append(f"✅ «{title}»: написала и закрепила")
        except Exception:
            done.append(f"✅ «{title}»: написала (закрепить не смогла — дайте боту право закреплять)")
    await message.answer("\n".join(done) or "Групп клуба нет — назначьте тариф в /channels.")


@router.chat_member(F.chat.type.in_(CLUB_CHAT_TYPES))
async def member_changed(update: ChatMemberUpdated, bot: Bot) -> None:
    """Вступила в канал клуба — купила; вышла (Tribute убрал по окончании подписки) —
    тренировки клуба ей больше не приходят."""
    plan = {r["chat_id"]: r["plan"] for r in await db.club_channels()}.get(update.chat.id)
    if plan not in config.PLANS:
        return
    tg_id = update.new_chat_member.user.id
    was_in = update.old_chat_member.status in IN_CHANNEL
    now_in = update.new_chat_member.status in IN_CHANNEL
    if now_in and not was_in:
        status = await payments.mark_club_member(bot, tg_id, plan, update.chat.id)
        if status is not None and status != sender.OK and update.chat.type != "channel":
            await hello_in_group(bot, update.chat.id, update.new_chat_member.user)
    elif was_in and update.new_chat_member.status in ("left", "kicked") and await db.club_deactivate(tg_id):
        await messages.purge_media(bot, tg_id, club_only=True)   # видео клуба — только пока подписка
        u = await db.get_user(tg_id)
        name = messages.name_of(u)
        await messages.say(bot, tg_id, texts.CLUB_ENDED.format(name=name))
        await messages.notify_admins(bot, f"🚪 {name} (id {tg_id}) вышла из клуба "
                                          f"«{html.escape(update.chat.title or '')}» — тренировки клуба ей остановлены.")
