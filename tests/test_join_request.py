"""Повтор заявки в канал не даёт второго приветствия и ложной тревоги админам."""
import asyncio
from types import SimpleNamespace

from app.handlers import channel


def _req(approve_error=None):
    calls = SimpleNamespace(approved=0)

    async def approve():
        calls.approved += 1
        if approve_error:
            raise Exception(approve_error)

    req = SimpleNamespace(
        chat=SimpleNamespace(id=-100, title="Канал"),
        from_user=SimpleNamespace(id=7, first_name="Алина", username="a"),
        user_chat_id=7, invite_link=None, approve=approve)
    return req, calls


class _Bot:
    def __init__(self):
        self.sent = []

    async def me(self):
        return SimpleNamespace(username="bot")

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def test_duplicate_and_already_participant(monkeypatch):
    alerts = []

    async def notify(bot, text, **kw):
        alerts.append(text)

    monkeypatch.setattr(channel.messages, "notify_admins", notify)
    channel._recent.clear()
    bot = _Bot()
    req, calls = _req()
    asyncio.run(channel.join_request(req, bot))
    asyncio.run(channel.join_request(req, bot))  # повтор того же обновления
    assert calls.approved == 1 and len(bot.sent) == 1
    assert len(alerts) == 1 and "одобрена" in alerts[0]

    channel._recent.clear()
    alerts.clear()
    req, _ = _req("Telegram server says - Bad Request: USER_ALREADY_PARTICIPANT")
    asyncio.run(channel.join_request(req, bot))
    assert alerts == []  # уже в канале — не тревога
