"""Личная тренировка по анкете: какая программа подходит сестре.

Идём сверху вниз — первая подходящая строка. Сначала ограничения по здоровью
(безопасность важнее цели), потом цель и опыт. Порядок проверен по таблице
противопоказаний: «беременность» и «давление» безопасны при любом ограничении,
«спина» нельзя при больных коленях (упражнения на четвереньках), «похудеть» —
только без ограничений (прыжки, бёрпи, выпады).
"""
from __future__ import annotations

PROGRAMS = {
    "pregnancy": "Беременность",
    "postpartum": "После родов и диастаз",
    "pressure": "При давлении",
    "knees": "Бережно для коленей",
    "back": "Для спины",
    "varicose": "При варикозе",
    "lose": "Похудеть",
    "tone": "Тонус",
}


def pick(goal: str | None, level: str | None, limits: str | None) -> str:
    """Ключ программы по ответам анкеты (users.goal, users.level, health.limits)."""
    lim = {x for x in (limits or "").split(",") if x}
    if "pregnant" in lim:
        return "pregnancy"
    if lim & {"postpartum", "diastasis"}:
        return "postpartum"
    if "pressure" in lim:
        return "pressure"
    if "knees" in lim:
        return "knees"
    if "back" in lim:
        return "back"
    if "varicose" in lim:
        return "varicose"
    if "other" in lim:
        return "postpartum"          # самая мягкая; тренер всё равно читает её сообщение
    if goal == "lose":
        return "tone" if level == "new" else "lose"
    if goal == "back":
        return "back"
    return "tone"
