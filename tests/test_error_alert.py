"""Ошибка в обработчике → админам приходит «⚠️ Ошибка в боте»."""
import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher, Router
from aiogram.types import Chat, Message, Update, User

from app import main, messages


def test_handler_error_notifies_admins(monkeypatch):
    sent = []

    async def fake_notify(bot, text, markup=None):
        sent.append(text)

    monkeypatch.setattr(messages, "notify_admins", fake_notify)
    main._last_alert.clear()
    dp = Dispatcher()
    r = Router()

    @r.message()
    async def boom(message: Message) -> None:
        raise ValueError("сломалось")

    dp.include_router(r)
    dp.errors.register(main.on_error)
    bot = Bot("123456:" + "A" * 35)
    msg = Message(message_id=1, date=datetime.now(), chat=Chat(id=7, type="private"),
                  from_user=User(id=7, is_bot=False, first_name="Аиша"), text="/start")
    for _ in range(2):                       # вторая такая же ошибка — без повтора
        asyncio.run(dp.feed_update(bot, Update(update_id=1, message=msg)))
    assert len(sent) == 1
    assert "Аиша (id 7)" in sent[0] and "ValueError" in sent[0] and "/start" in sent[0]
