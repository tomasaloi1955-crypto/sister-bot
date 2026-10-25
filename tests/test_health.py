"""Вопросы о здоровье: разбор роста/веса и сборка советов."""
from app import texts
from app.handlers.health import advice_text, parse_params


def test_parse_params():
    assert parse_params("165 60") == (165, 60)
    assert parse_params("рост 165, вес 60") == (165, 60)
    assert parse_params("60 165") == (165, 60)
    assert parse_params("165/60") == (165, 60)
    assert parse_params("165") is None
    assert parse_params("привет") is None
    assert parse_params("5 6") is None


def test_advice_includes_limits_age_and_nutrition():
    h = {"limits": "back,postpartum", "age": "45p"}
    text = advice_text(h, "lose")
    assert texts.LIMIT_ADVICE["back"] in text and texts.LIMIT_ADVICE["postpartum"] in text
    assert texts.AGE_ADVICE["45p"] in text and texts.NUTRITION["lose"] in text
    assert text.endswith(texts.HEALTH_DISCLAIMER)


def test_no_limits_gives_only_nutrition():
    text = advice_text({"limits": "none", "age": "25_35"}, "energy")
    assert texts.NUTRITION["energy"] in text and "Спина" not in text
