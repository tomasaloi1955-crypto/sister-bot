"""Вопросы о здоровье: разбор роста/веса, ИМТ и сборка советов."""
import asyncio

from app import ai_advice, config, texts
from app.handlers.health import advice_text, build_advice, parse_params


def H(**kw):
    base = {"limits": "", "age": "25_35", "note": None, "height_cm": None, "weight_kg": None,
            "consent_at": "yes"}
    return {**base, **kw}


def test_parse_params():
    assert parse_params("165 60") == (165, 60)
    assert parse_params("рост 165, вес 60") == (165, 60)
    assert parse_params("60 165") == (165, 60)
    assert parse_params("165/60") == (165, 60)
    assert parse_params("165") is None
    assert parse_params("привет") is None
    assert parse_params("5 6") is None


def test_bmi_groups():
    assert ai_advice.bmi_group(165, 48)[1] == "low"
    assert ai_advice.bmi_group(165, 60)[1] == "ok"
    assert ai_advice.bmi_group(165, 75)[1] == "over"
    assert ai_advice.bmi_group(160, 85)[1] == "high"
    assert ai_advice.bmi_group(None, 60) is None


def test_advice_limits_age_and_weight():
    text = advice_text(H(limits="back,diastasis", age="45p", height_cm=160, weight_kg=72), "lose")
    assert texts.LIMIT_ADVICE["back"] in text and texts.LIMIT_ADVICE["diastasis"] in text
    assert texts.AGE_ADVICE["45p"] in text and texts.WEIGHT_ADVICE["over"] in text
    assert texts.NUTRITION["lose"] not in text            # есть вес — общий совет о питании не нужен
    assert text.endswith(texts.HEALTH_DISCLAIMER)


def test_no_weight_gives_nutrition_by_goal():
    text = advice_text(H(limits="none"), "energy")
    assert texts.NUTRITION["energy"] in text and "Спина" not in text


def test_postpartum_and_pregnant_no_diet():
    assert texts.WEIGHT_ADVICE["postpartum"] in advice_text(
        H(limits="postpartum", height_cm=160, weight_kg=80), "lose")
    text = advice_text(H(limits="pregnant", height_cm=160, weight_kg=80), "lose")
    assert "⚖️" not in text


def test_other_with_note():
    text = advice_text(H(limits="other", note="варикоз <ног>"), None)
    assert "варикоз &lt;ног&gt;" in text and texts.LIMIT_ADVICE["other"] not in text


def test_without_key_falls_back(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    h = H(limits="varicose", height_cm=165, weight_kg=60)
    assert asyncio.run(build_advice(h, "tone")) == advice_text(h, "tone")


def test_gemini_answer_is_used(monkeypatch):
    async def fake(h, goal):
        return ai_advice.clean("🔹 Варикоз: вместо прыжков — ходьба.\n**⚖️ Вес:** в норме <3")
    monkeypatch.setattr(ai_advice, "advise", fake)
    text = asyncio.run(build_advice(H(limits="varicose"), None))
    assert "<b>🔹 Варикоз:</b> вместо прыжков" in text and "&lt;3" in text and "**" not in text


def test_profile_for_gemini():
    p = ai_advice.profile(H(limits="back,other", note="варикоз", height_cm=160, weight_kg=72), "lose")
    assert "Спина, грыжа" in p and "варикоз" in p and "ИМТ 28.1" in p and "Похудеть" in p
