"""Список тёплых для тренера, «сделала» текстом, подарок."""
from datetime import datetime, timedelta, timezone

from app import payments, tick
from app.handlers.user import DONE_WORDS

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)


def _u(tg, day, paid=False, name="Надя"):
    return {"tg_id": tg, "first_name": name, "username": None, "tz": "Asia/Almaty",
            "started_at": NOW - timedelta(days=day - 1), "paid_at": NOW if paid else None}


def test_warm_lines_hot_first():
    users = [_u(1, 7), _u(2, 7, name="Аня"), _u(3, 6), _u(4, 7, paid=True)]
    info = {1: {"done": 0, "mood": None, "review": None, "clicked": False},
            2: {"done": 2, "mood": "great", "review": "спина не болит", "clicked": True}}
    rows = tick.warm_lines(users, info, NOW)
    assert [u["tg_id"] for _, _, u in rows] == [2, 1]          # день 6 и купившая — не в списке
    assert rows[0][1].startswith("🔥") and "смотрела тарифы" in rows[0][1] and "спина" in rows[0][1]
    assert rows[1][1].startswith("•")


def test_done_words():
    for t in ("сделала", "Сделала!", "+", "✅", "готово 👍", "сделала тренировку"):
        assert DONE_WORDS.match(t), t
    for t in ("сделала, но болит колено", "а сколько стоит клуб?"):
        assert not DONE_WORDS.match(t), t


def test_gift_note_days_9_10():
    assert "🎁" in payments.gift_note(_u(1, 9), NOW)
    assert "🎁" in payments.gift_note(_u(1, 10), NOW)
    assert payments.gift_note(_u(1, 8), NOW) == "" and payments.gift_note(_u(1, 11), NOW) == ""
