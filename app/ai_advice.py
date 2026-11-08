"""Gemini пишет сестре короткие советы по её ответам о здоровье.

Ключа нет, Gemini не ответил или ответил странно — возвращаем None, и бот шлёт
готовые советы из texts.py. Анкета от этого никогда не ломается.
"""
from __future__ import annotations

import html
import logging

import aiohttp

from . import config, texts

log = logging.getLogger(__name__)

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

RULES = """Ты — заботливый женский фитнес-тренер в клубе домашних тренировок для сестёр-мусульманок.
По анкете напиши сестре очень короткие советы на русском, обращайся на «ты», тепло.

Правила:
- На каждую проблему здоровья — ровно ОДНО предложение в формате «не делай X — замени на Y».
  Примеры: при диастазе не делай скручивания — замени на тазовый мостик; при варикозе вместо прыжков — ходьба;
  при грыже не делай скручивания и наклоны с круглой спиной — замени на «кошку» и тазовый мостик.
- Если указаны рост и вес — ОДНО предложение про вес (ИМТ уже посчитан) и что ограничить в еде.
  После родов и при беременности — никаких диет, только мягко убрать сладкое.
- Если роста и веса нет — ОДНО предложение о питании под её цель.
- Не ставь диагнозов. При грыже, давлении, беременности добавь в то же предложение «сначала спроси врача».
- Каждый совет — отдельная строка, начинается с «🔹 Название:» (для веса «⚖️ Вес:», для питания «🍽 Питание:»).
- Не больше 5 строк, без вступления и без подписи, без markdown (без звёздочек и решёток)."""


def bmi_group(height_cm: int | None, weight_kg: int | None) -> tuple[float, str] | None:
    """ИМТ и группа: low (<18.5), ok (<25), over (<30), high (30+)."""
    if not height_cm or not weight_kg:
        return None
    bmi = weight_kg / (height_cm / 100) ** 2
    group = "low" if bmi < 18.5 else "ok" if bmi < 25 else "over" if bmi < 30 else "high"
    return round(bmi, 1), group


def profile(h, goal: str | None) -> str:
    """Анкета сестры — тем же языком, что и кнопки в боте."""
    keys = [x for x in (h["limits"] or "").split(",") if x and x not in ("none", "other")]
    lines = [f"Возраст: {texts.AGES.get(h['age'], 'не указан')}",
             "Здоровье: " + (", ".join(texts.LIMITS[k] for k in keys if k in texts.LIMITS) or "ничего не отметила")]
    if h["note"]:
        lines.append(f"Написала своими словами: {h['note']}")
    b = bmi_group(h["height_cm"], h["weight_kg"])
    if b:
        names = {"low": "ниже нормы", "ok": "норма", "over": "небольшой лишний вес", "high": "выше нормы"}
        lines.append(f"Рост {h['height_cm']} см, вес {h['weight_kg']} кг, ИМТ {b[0]} — {names[b[1]]}")
    else:
        lines.append("Рост и вес не указала")
    lines.append("Цель: " + texts.GOALS.get(goal, "не указана").split(" ", 1)[-1])
    return "\n".join(lines)


def clean(text: str) -> str | None:
    """Ответ Gemini → безопасный HTML для Telegram. Название совета — жирным."""
    out = []
    for line in text.replace("*", "").replace("#", "").splitlines():
        line = line.strip()
        if not line:
            continue
        head, sep, rest = line.partition(":")
        if sep and len(head) <= 40:
            out.append(f"<b>{html.escape(head)}:</b>{html.escape(rest)}")
        else:
            out.append(html.escape(line))
    if not out or len(out) > 7 or sum(map(len, out)) > 1200:
        return None
    return "\n\n".join(out)


async def advise(h, goal: str | None) -> str | None:
    if not config.GEMINI_API_KEY:
        return None
    body = {
        "systemInstruction": {"parts": [{"text": RULES}]},
        "contents": [{"role": "user", "parts": [{"text": profile(h, goal)}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 500,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(URL.format(model=config.GEMINI_MODEL), json=body,
                              headers={"x-goog-api-key": config.GEMINI_API_KEY}) as r:
                data = await r.json(content_type=None)
                if r.status != 200:
                    log.warning("Gemini ответил %s: %s", r.status, str(data)[:300])
                    return None
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except Exception as e:  # сеть, таймаут, пустой ответ — шлём готовые советы
        log.warning("Gemini недоступен: %r", e)
        return None
    return clean(text)
