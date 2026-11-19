"""SisterFit только для сестёр: мужские имена и ники не пускаем."""
import asyncio
from types import SimpleNamespace

from app import male_names
from app.handlers import channel


def test_looks_male():
    m = male_names.looks_male
    assert m("Ахмед")
    assert m("Иван", "Петров")
    assert m(None, None, "Dmitry_1990")
    assert m("Магомед")
    assert m("🌙", None, "ahmedAliev")
    assert m("Ля", "Алиев")
    # сёстры и сомнительные — пропускаем
    assert not m("Амина")
    assert not m("Алина", "Иванова")
    assert not m("Aisha", None, "aisha_ali")
    assert not m("Саша")
    assert not m("Женя")
    assert not m("Нур")
    assert not m("Марина")
    assert not m("Умм Мухаммад")
    assert not m("🌸", None, None)


def test_male_join_request_declined(monkeypatch):
    alerts = []

    async def notify(bot, text, **kw):
        alerts.append(text)

    monkeypatch.setattr(channel.messages, "notify_admins", notify)
    channel._recent.clear()
    calls = SimpleNamespace(approved=0, declined=0)

    async def approve():
        calls.approved += 1

    async def decline():
        calls.declined += 1

    sent = []

    class Bot:
        async def me(self):
            return SimpleNamespace(username="bot")

        async def send_message(self, chat_id, text, **kw):
            sent.append(text)

    req = SimpleNamespace(
        chat=SimpleNamespace(id=-100, title="SisterFit"),
        from_user=SimpleNamespace(id=8, first_name="Ахмед", last_name=None, username="ahmed"),
        user_chat_id=8, invite_link=None, approve=approve, decline=decline)
    asyncio.run(channel.join_request(req, Bot()))
    assert calls.declined == 1 and calls.approved == 0 and sent == []
    assert len(alerts) == 1 and "отклонена" in alerts[0] and "SisterFit" in alerts[0]
