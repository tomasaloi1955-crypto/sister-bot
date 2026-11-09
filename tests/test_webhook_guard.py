from app.webhook_guard import decide

NEW = "https://sister-bot-e5q4.onrender.com/webhook"
OLD = "https://sister-bot.onrender.com/webhook"


def test_decide():
    assert decide("", OLD, False, None) == "take"               # вебхука нет
    assert decide(OLD, OLD, False, None) == "take"              # уже наш
    assert decide(NEW, OLD, False, None) == "check"             # чужой — проверить
    assert decide(NEW, OLD, False, True) == "keep"              # там живой бот — не трогаем
    assert decide(NEW, OLD, False, False) == "take"             # там пусто — берём
    assert decide(NEW, OLD, True, None) == "take"               # TAKE_WEBHOOK=1 — переезд
