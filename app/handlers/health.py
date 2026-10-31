"""Вопросы о здоровье после анкеты: возраст, ограничения, рост и вес.

Только с согласия и с кнопкой «Пропустить». Бот не ставит диагнозов: он шлёт
советы, которые написала тренер (texts.py), и сообщает тренеру об ограничениях.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from .. import config, db, funnel, keyboards, messages, texts

router = Router(name="health")
router.message.filter(F.chat.type == "private")


def _limits(h) -> list[str]:
    return [x for x in (h["limits"] or "").split(",") if x] if h else []


async def ask_consent(bot: Bot, tg_id: int, onboarding: bool) -> None:
    await db.set_health(tg_id, step="consent", onboarding=onboarding)
    await messages.say(bot, tg_id, texts.HEALTH_CONSENT.format(coach=config.COACH_NAME),
                       keyboards.health_consent())


async def finish_onboarding(bot: Bot, u) -> None:
    """Анкета закончена: чат + первая тренировка сразу. Ключ пишем в журнал,
    чтобы тик не прислал ту же тренировку второй раз."""
    ready = 1 in await db.workout_days()
    # Видео 1 ещё нет — не обещаем «ниже»: тренировку пришлёт тик, когда она появится.
    await messages.say(bot, u["tg_id"], (texts.SURVEY_DONE if ready else texts.SURVEY_DONE_WAIT).format(tz=u["tz"]))
    if config.FREE_CHAT_LINK:
        await messages.say(bot, u["tg_id"], texts.FREE_CHAT.format(link=config.FREE_CHAT_LINK),
                           keyboards.free_chat())
    slot = funnel.slots_of(u["slot"])[0]
    if ready and await db.claim_send(u["tg_id"], f"workout:1:{slot}"):
        await messages.send_workout(bot, u["tg_id"], 1, slot)


def advice_text(h, goal: str | None) -> str:
    parts = [texts.LIMIT_ADVICE[x] for x in texts.LIMIT_ADVICE if x in _limits(h)]
    if h["age"] in texts.AGE_ADVICE:
        parts.append(texts.AGE_ADVICE[h["age"]])
    if goal in texts.NUTRITION:
        parts.append(texts.NUTRITION[goal])
    return texts.HEALTH_ADVICE_HEADER + "\n\n".join(parts) + texts.HEALTH_DISCLAIMER if parts else ""


async def _finish(bot: Bot, tg_id: int) -> None:
    h = await db.set_health(tg_id, step=None)
    u = await db.get_user(tg_id)
    if h["consent_at"]:
        text = advice_text(h, u["goal"])
        if text:
            await messages.say(bot, tg_id, text)
        limits = [x for x in _limits(h) if x != "none"]
        if limits:
            who = messages.name_of(u) + (f" @{u['username']}" if u["username"] else "")
            params = f"; {h['height_cm']} см / {h['weight_kg']} кг" if h["height_cm"] else ""
            await messages.notify_admins(
                bot, f"🩺 {who} (id {tg_id}): возраст {texts.AGES.get(h['age'], '—')}; "
                     + ", ".join(texts.LIMITS[x] for x in limits) + params)
    if h["onboarding"]:
        await db.set_health(tg_id, onboarding=False)
        await finish_onboarding(bot, u)
    else:
        await messages.say(bot, tg_id, texts.HEALTH_SAVED)


async def _mark_choice(cb: CallbackQuery, label: str) -> None:
    try:
        await cb.message.edit_text(f"{cb.message.html_text}\n\n<b>→ {html.escape(label)}</b>")
    except Exception:
        pass


@router.callback_query(F.data == "h:start")
async def start(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    await ask_consent(bot, cb.from_user.id, onboarding=False)


@router.callback_query(F.data.in_({"h:yes", "h:skip"}))
async def consent(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    if cb.data == "h:skip":
        await _mark_choice(cb, texts.HEALTH_SKIP_BUTTON)
        await _finish(bot, cb.from_user.id)
        return
    await _mark_choice(cb, texts.HEALTH_YES_BUTTON)
    await db.set_health(cb.from_user.id, consent_at=datetime.now(timezone.utc), step="age")
    await cb.message.answer(texts.Q_AGE, reply_markup=keyboards.choices("h:age", texts.AGES))


@router.callback_query(F.data.startswith("h:age:"))
async def age(cb: CallbackQuery) -> None:
    value = cb.data.split(":", 2)[2]
    await cb.answer()
    if value not in texts.AGES:
        return
    await _mark_choice(cb, texts.AGES[value])
    await db.set_health(cb.from_user.id, age=value, limits="", step="limits")
    await cb.message.answer(texts.Q_LIMITS, reply_markup=keyboards.health_limits(set()))


@router.callback_query(F.data.startswith("h:lim:"))
async def toggle_limit(cb: CallbackQuery) -> None:
    key = cb.data.split(":", 2)[2]
    await cb.answer()
    if key not in texts.LIMITS:
        return
    selected = set(_limits(await db.get_health(cb.from_user.id)))
    if key in selected:
        selected.discard(key)
    elif key == "none":
        selected = {"none"}
    else:
        selected = (selected - {"none"}) | {key}
    await db.set_health(cb.from_user.id, limits=",".join(k for k in texts.LIMITS if k in selected))
    try:
        await cb.message.edit_reply_markup(reply_markup=keyboards.health_limits(selected))
    except Exception:
        pass


@router.callback_query(F.data == "h:limdone")
async def limits_done(cb: CallbackQuery) -> None:
    await cb.answer()
    h = await db.set_health(cb.from_user.id, step="params")
    chosen = ", ".join(texts.LIMITS[x] for x in _limits(h)) or texts.LIMITS["none"]
    try:
        await cb.message.edit_text(f"{cb.message.html_text}\n\n<b>→ {html.escape(chosen)}</b>")
    except Exception:
        pass
    await cb.message.answer(texts.Q_PARAMS, reply_markup=keyboards.params_skip())


@router.callback_query(F.data == "h:pskip")
async def params_skip(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    h = await db.get_health(cb.from_user.id)
    if not h or h["step"] != "params":
        return
    await _mark_choice(cb, texts.PARAMS_SKIP_BUTTON)
    await _finish(bot, cb.from_user.id)


def parse_params(text: str) -> tuple[int, int] | None:
    """«165 60», «165/60», «рост 165 вес 60» → (рост, вес). Порядок угадываем по диапазонам."""
    nums = [int(x) for x in re.findall(r"\d{2,3}", text)]
    if len(nums) != 2:
        return None
    for height, weight in (nums, nums[::-1]):
        if 120 <= height <= 220 and 30 <= weight <= 250:
            return height, weight
    return None


async def handle_text(message: Message, bot: Bot) -> bool:
    """Свободный текст, когда ждём рост и вес. True — сообщение обработано здесь."""
    h = await db.get_health(message.from_user.id)
    if not h or h["step"] != "params":
        return False
    parsed = parse_params(message.text)
    if parsed is None:
        await message.answer(texts.PARAMS_BAD, reply_markup=keyboards.params_skip())
        return True
    await db.set_health(message.from_user.id, height_cm=parsed[0], weight_kg=parsed[1])
    await _finish(bot, message.from_user.id)
    return True
