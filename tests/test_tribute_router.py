"""Диспетчер вебхука Tribute: кому отдавать уведомление."""
from app import config
from app.tribute_router import decide, product_keys, target_url


def test_product_keys():
    assert product_keys({"product_id": 5, "subscription_id": None, "period_id": 7}) == ["5", "7"]
    assert product_keys({}) == []


def test_env_beats_db_and_unknown_is_none():
    assert decide(["31"], {"31": "club3x"}, {"31": "school"}) == "club3x"
    assert decide(["40", "41"], {}, {"41": "ignore"}) == "ignore"
    assert decide(["40"], {}, {"40": None}) is None
    assert decide([], {}, {}) is None


def test_target_url():
    assert target_url("school") == config.SCHOOL_WEBHOOK_URL
    assert target_url("url:https://x.example/hook") == "https://x.example/hook"
    assert target_url("ignore") is None and target_url("club3x") is None
