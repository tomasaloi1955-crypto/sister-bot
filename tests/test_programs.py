"""Выбор личной тренировки по анкете: python -m pytest tests"""
from app.programs import PROGRAMS, pick


def test_health_first():
    assert pick("lose", "now", "pregnant,knees") == "pregnancy"
    assert pick("lose", "now", "diastasis") == "postpartum"
    assert pick("tone", "before", "postpartum,back") == "postpartum"
    assert pick("lose", "now", "pressure,back") == "pressure"
    assert pick("back", "new", "back,knees") == "knees"        # «спина» нельзя при коленях
    assert pick("tone", "new", "back") == "back"
    assert pick("tone", "new", "varicose") == "varicose"
    assert pick("lose", "now", "other") == "postpartum"


def test_goal_when_healthy():
    assert pick("lose", "before", "none") == "lose"
    assert pick("lose", "new", "") == "tone"                   # новичкам сначала тонус
    assert pick("back", "now", None) == "back"
    assert pick("tone", "before", "none") == "tone"
    assert pick("energy", "new", "none") == "tone"
    assert pick(None, None, None) == "tone"                    # анкета не пройдена


def test_every_result_is_a_program():
    for lim in ("", "pregnant", "postpartum", "pressure", "knees", "back", "varicose", "other"):
        for goal in ("lose", "tone", "back", "energy", None):
            assert pick(goal, "new", lim) in PROGRAMS
