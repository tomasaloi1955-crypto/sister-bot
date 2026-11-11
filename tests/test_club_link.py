"""Ссылка в клуб: если канала нет в настройках — берём группу тарифа из /channels."""
import asyncio
from types import SimpleNamespace

from app import config, payments


def test_link_from_channels(monkeypatch):
    sent, made = [], []

    async def channels():
        return [{"chat_id": -100, "plan": "3x"}, {"chat_id": -200, "plan": "6x"}]

    async def say(bot, tg_id, text, *a, **k):
        sent.append(text)

    class Bot:
        async def create_chat_invite_link(self, chat_id, **kw):
            made.append(chat_id)
            return SimpleNamespace(invite_link=f"https://t.me/+x{chat_id}")

    monkeypatch.setitem(config.PLANS["3x"], "chat", "")
    monkeypatch.setattr(payments.db, "club_channels", channels)
    monkeypatch.setattr(payments.messages, "say", say)
    link = asyncio.run(payments.send_club_link(Bot(), 7, "3x"))
    assert made == [-100] and link == "https://t.me/+x-100" and "https://t.me/+x-100" in sent[0]
