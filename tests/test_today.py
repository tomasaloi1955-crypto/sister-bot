"""/today: разбор дня и сводка «кто что сделал»."""
from datetime import date, datetime, timezone

from app.handlers.admin import parse_day, today_report

T = date(2026, 11, 10)


def test_parse_day():
    assert parse_day(None, T) == T
    assert parse_day("сегодня", T) == T
    assert parse_day("Вчера", T) == date(2026, 11, 9)
    assert parse_day("позавчера", T) == date(2026, 11, 8)
    assert parse_day("08.11", T) == date(2026, 11, 8)
    assert parse_day("31.12", T) == date(2025, 12, 31)
    assert parse_day("08.11.26", T) == date(2026, 11, 8)
    assert parse_day("32.11", T) is None
    assert parse_day("ерунда", T) is None


def test_report():
    at = datetime(2026, 11, 10, 5, 40, tzinfo=timezone.utc)
    rows = [
        {"tg_id": 1, "club": True, "num": 12, "created_at": at, "first_name": "Надя", "username": "nadya"},
        {"tg_id": 2, "club": False, "num": 3, "created_at": at, "first_name": "Аня", "username": None},
        {"tg_id": 1, "club": False, "num": 1, "created_at": at, "first_name": "Надя", "username": "nadya"},
    ]
    text = today_report(T, rows, "Сегодня")
    assert "нажали «Сделала»: 2 (отметок 3)" in text
    assert "Надя @nadya — клуб №12" in text and "бесплатная №1 (" in text
    assert "Аня — бесплатная №3" in text
    assert "никто" in today_report(T, [], "Сегодня")


def test_day_report_text():
    from app.handlers.admin import day_report_text
    at = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
    u = lambda tg, name: {"tg_id": tg, "first_name": name, "username": None}
    rep = {"sends": [{**u(1, "Надя"), "key": "sale:7", "sent_at": at},
                     {**u(2, "Аня"), "key": "sale:7", "sent_at": at},
                     {**u(2, "Аня"), "key": "club_click", "sent_at": at},
                     {**u(3, "Лейла"), "key": "workout:3:evening", "sent_at": at}],
           "done": [], "feedback": [], "paid": [], "new": [], "blocked": [u(4, "Мира")],
           "inbox": [{**u(1, "Надя"), "text": "сколько стоит?", "created_at": at}]}
    text = day_report_text(rep, "Сегодня")
    assert "Предложение клуба (день 7) — <b>2</b>: Надя, Аня" in text
    assert "Открыли тарифы — <b>1</b>: Аня" in text and "club_click" not in text
    assert "Тренировки бесплатной недели — <b>1</b>" in text
    assert "«сколько стоит?»" in text and "Заблокировали бота — <b>1</b>: Мира" in text
