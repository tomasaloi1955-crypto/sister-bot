"""Каналы клуба: вступила в канал клуба — значит, купила (впускает туда Tribute).

Бота добавляют администратором в канал → он спрашивает админа кнопками, чей это
канал (клуб 3 раза / клуб каждый день / не клуб). Дальше, когда в канал клуба
вступает человек, бот отмечает покупку, и продающие сообщения ей прекращаются.
Запасная проверка — payments.club_plan_by_membership() перед продажей в тике.
"""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup

from .. import config, db, messages, payments, texts

log = logging.getLogger(__name__)
router = Router(name="club_channels")
admin_only = F.from_user.id.in_(config.ADMIN_IDS)

PLAN_LABELS = {"3x": "🌿 Клуб 3 раза в неделю", "6x": "🔥 Клуб каждый день", "none": "📣 Не клуб"}
IN_CHANNEL = {"member", "restricted"}        # админы и создатель — не покупательницы


def _kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[B(text=label, callback_data=f"adm:ch:{code}:{chat_id}")]
                                                 for code, label in PLAN_LABELS.items()])


@router.my_chat_member(F.chat.type == "channel")
async def bot_added(update: ChatMemberUpdated, bot: Bot) -> None:
    """Бота сделали администратором канала — спросить, чей канал."""
    if update.new_chat_member.status != "administrator":
        return
    known = {r["chat_id"]: r["plan"] for r in await db.club_channels()}
    await db.remember_channel(update.chat.id, update.chat.title)
    if known.get(update.chat.id):
        return
    await messages.notify_admins(
        bot, f"📢 Бот стал администратором канала «{html.escape(update.chat.title or '')}». Что это за канал?",
        _kb(update.chat.id))


@router.message(admin_only, StateFilter(None), F.forward_origin.chat.type == "channel")
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


@router.callback_query(admin_only, F.data.startswith("adm:ch:"))
async def channel_plan(cb: CallbackQuery) -> None:
    _, _, code, chat_id = cb.data.split(":", 3)
    if code not in PLAN_LABELS:
        return await cb.answer()
    await db.set_channel_plan(int(chat_id), code)
    await cb.answer("Запомнила")
    await cb.message.edit_text(cb.message.html_text + f"\n\n→ {PLAN_LABELS[code]}")


@router.chat_member(F.chat.type == "channel")
async def member_changed(update: ChatMemberUpdated, bot: Bot) -> None:
    """Кто-то вступил в канал: если это канал клуба — отмечаем покупку."""
    if update.new_chat_member.status not in IN_CHANNEL or update.old_chat_member.status in IN_CHANNEL:
        return
    plan = {r["chat_id"]: r["plan"] for r in await db.club_channels()}.get(update.chat.id)
    if plan in config.PLANS:
        await payments.mark_club_member(bot, update.new_chat_member.user.id, plan, update.chat.id)
