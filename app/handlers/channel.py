"""Заявки на вступление в рекламный канал: бот лично здоровается и зовёт в воронку.

Канал впускает по ссылке «с заявкой». Telegram разрешает боту написать
человеку, подавшему заявку (5 минут, пока заявка не одобрена), — пишем
приветствие с кнопкой «Начать», потом одобряем заявку. Название ссылки-заявки
(латиницей) становится меткой источника: ?start=src_<название>.
"""
from __future__ import annotations

import html
import logging
import re

from aiogram import Bot, Router
from aiogram.types import ChatJoinRequest, InlineKeyboardButton, InlineKeyboardMarkup

from .. import config, messages, texts

log = logging.getLogger(__name__)
router = Router(name="channel")


def source_from_link(name: str | None) -> str:
    """Название ссылки-заявки → метка для /start (латиница, цифры, _ и -)."""
    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip()).strip("_")[:50]
    return clean or "kanal"


@router.chat_join_request()
async def join_request(req: ChatJoinRequest, bot: Bot) -> None:
    source = source_from_link(req.invite_link.name if req.invite_link else None)
    me = await bot.me()
    url = f"https://t.me/{me.username}?start=src_{source}"
    name = html.escape(req.from_user.first_name or "сестра")
    welcomed = approved = False
    try:
        await bot.send_message(
            req.user_chat_id, texts.JOIN_WELCOME.format(name=name, coach=config.COACH_NAME),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=texts.JOIN_BUTTON, url=url)]]))
        welcomed = True
    except Exception:
        log.exception("Не удалось написать подавшей заявку %s", req.from_user.id)
    try:
        await req.approve()
        approved = True
    except Exception as e:
        log.exception("Не удалось одобрить заявку %s", req.from_user.id)
        await messages.notify_admins(
            bot, f"⚠️ Не смогла одобрить заявку {name} в «{html.escape(req.chat.title or str(req.chat.id))}»: "
                 f"{html.escape(str(e))[:300]}\nОдобрите вручную и проверьте, что у бота в этом чате "
                 "включено право «Пригласительные ссылки» / «Добавление участников».")
    log.info("Заявка в канал %s от %s, метка %s", req.chat.id, req.from_user.id, source)
    if approved:
        who = name + (f" @{req.from_user.username}" if req.from_user.username else "")
        await messages.notify_admins(
            bot, f"📥 Заявка в «{html.escape(req.chat.title or '')}»: {who} (id {req.from_user.id}), "
                 f"ссылка «{html.escape(source)}» — ✅ одобрена"
                 + ("" if welcomed else ", ⚠️ приветствие не доставлено"))
