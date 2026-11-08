"""Сценарии участницы: /start, анкета, «Сделала», отзывы, настройки, оплата."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from .. import config, db, funnel, keyboards, messages, payments, texts
from . import club_channels, health

log = logging.getLogger(__name__)
router = Router(name="user")
# Только личка: в группах и каналах клуба бот молчит (он там администратор и видит все сообщения).
router.message.filter(F.chat.type == "private")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _source(payload: str | None) -> str | None:
    """/start src_kanal1 → kanal1. Любая другая метка сохраняется как есть."""
    if not payload:
        return None
    payload = payload.strip()[:64]
    if payload.startswith("src_"):
        payload = payload[4:]
    return payload or None


async def _notify_new_lead(bot: Bot, u) -> None:
    """Владелице: новая девушка нажала /start (первый раз за всё время)."""
    who = messages.name_of(u)
    if u["username"]:
        who += f" @{html.escape(u['username'])}"
    source = html.escape(u["source"] or "без метки")
    await messages.notify_admins(
        bot, f"🆕 Новый лид (бесплатная неделя): {who} (id {u['tg_id']}), источник: {source}")


# ─────────────────────────────── /start и анкета ───────────────────────────────

@router.message(CommandStart())
async def start(message: Message, command: CommandObject, bot: Bot) -> None:
    fu = message.from_user
    from_club = (command.args or "").strip() == "club"     # кнопка «Получать тренировки» из группы клуба
    u = await db.upsert_user(fu.id, fu.username, fu.first_name, None if from_club else _source(command.args))
    name = messages.name_of(u)
    await club_channels.forget_group_hello(bot, fu.id)
    # В клубе (или уже в группе клуба, но бот не знал) — приветствие клуба вместо бесплатной недели
    member = await db.club_member(fu.id)
    if not (member and member["active"]):
        if await payments.club_plan_by_membership(bot, fu.id):
            return                                     # записали в клуб, приветствие ушло
    elif u["started_at"] is None or from_club:
        await payments.club_welcome(bot, fu.id, member["plan"])
        return
    if u["inserted"] and not from_club:
        await _notify_new_lead(message.bot, u)
    if u["started_at"]:
        day = funnel.funnel_day(u["started_at"], _now(), u["tz"])
        await message.answer(texts.WELCOME_BACK.format(name=name, day=day),
                             reply_markup=keyboards.free_chat())
        return
    await message.answer(texts.WELCOME.format(name=name, coach=config.COACH_NAME))
    await message.answer(texts.Q_GOAL, reply_markup=keyboards.choices("q:goal", texts.GOALS))


@router.callback_query(F.data.startswith("q:"))
async def survey(cb: CallbackQuery, bot: Bot) -> None:
    _, field, value = cb.data.split(":", 2)
    await cb.answer()
    u = await db.get_user(cb.from_user.id)
    if u is None:
        u = await db.upsert_user(cb.from_user.id, cb.from_user.username, cb.from_user.first_name, None)
    if field == "slot" and value == "now":
        if u["started_at"] is not None:      # старая кнопка у той, кто уже начала
            return
        await db.set_field(u["tg_id"], "first_now", True)
        try:
            await cb.message.edit_text(f"{cb.message.html_text}\n\n<b>→ {texts.SLOT_NOW}</b>")
        except Exception:
            pass
        await cb.message.answer(texts.Q_SLOT_AFTER_NOW, reply_markup=keyboards.choices("q:slot", texts.SLOTS))
        return
    options = {"goal": texts.GOALS, "level": texts.LEVELS, "slot": texts.SLOTS}
    if field not in options or value not in options[field]:
        return
    try:
        await cb.message.edit_text(f"{cb.message.html_text}\n\n<b>→ {options[field][value]}</b>")
    except Exception:
        pass
    if field == "goal":
        await db.set_field(u["tg_id"], "goal", value)
        await cb.message.answer(texts.Q_LEVEL, reply_markup=keyboards.choices("q:level", texts.LEVELS))
    elif field == "level":
        await db.set_field(u["tg_id"], "level", value)
        if u["started_at"] is None:
            await cb.message.answer(texts.Q_SLOT_FIRST, reply_markup=keyboards.first_slot())
        else:
            await cb.message.answer(texts.Q_SLOT, reply_markup=keyboards.choices("q:slot", texts.SLOTS))
    else:
        first_time = u["started_at"] is None
        u = await db.start_month(u["tg_id"], value)
        if first_time:
            await health.ask_consent(bot, u["tg_id"], onboarding=True)
        else:
            await cb.message.answer(texts.SAVED)


# ─────────────────────── предложение недели (/reweek) ───────────────────────

@router.callback_query(F.data == "reweek")
async def reweek(cb: CallbackQuery) -> None:
    """Кнопка из рассылки /reweek: начинаем анкету, дальше — обычная неделя."""
    fu = cb.from_user
    u = await db.get_user(fu.id) or await db.upsert_user(fu.id, fu.username, fu.first_name, None)
    if u["started_at"] is not None or u["paid_at"] is not None:   # уже в воронке или в клубе
        await cb.answer(texts.REWEEK_ALREADY, show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer(texts.WELCOME.format(name=messages.name_of(u), coach=config.COACH_NAME))
    await cb.message.answer(texts.Q_GOAL, reply_markup=keyboards.choices("q:goal", texts.GOALS))


# ─────────────────────────────── «Сделала!» ───────────────────────────────

@router.callback_query(F.data.startswith("done:"))
async def done(cb: CallbackQuery) -> None:
    day = int(cb.data.split(":")[1])
    u = await db.get_user(cb.from_user.id)
    if u is None or u["started_at"] is None:
        await cb.answer(texts.NOT_STARTED, show_alert=True)
        return
    if day > funnel.funnel_day(u["started_at"], _now(), u["tz"]):
        await cb.answer(texts.DONE_TOO_EARLY, show_alert=True)
        return
    new, total = await db.mark_done(u["tg_id"], day)
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=keyboards.done_marked())
    except Exception:
        pass
    await cb.message.answer((texts.DONE_REPLY if new else texts.DONE_REPLY_AGAIN).format(count=total))


@router.callback_query(F.data.startswith("cd:"))
async def club_done(cb: CallbackQuery) -> None:
    n = int(cb.data.split(":")[1])
    new, total = await db.club_mark_done(cb.from_user.id, n)
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=keyboards.done_marked())
    except Exception:
        pass
    await cb.message.answer((texts.DONE_REPLY if new else texts.DONE_REPLY_AGAIN).format(count=total))


@router.callback_query(F.data.startswith("rw:"))
async def rewatch(cb: CallbackQuery, bot: Bot) -> None:
    parts = cb.data.split(":")
    club = parts[1] == "c"
    n = int(parts[-1])
    u = await db.get_user(cb.from_user.id)
    sent, rewatched = await db.media_rewatch_state(cb.from_user.id, n, club)
    if u is None or not sent or rewatched:
        await cb.answer(texts.REWATCH_USED, show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await messages.say(bot, cb.from_user.id, texts.REWATCH_NOTE)
    if club:
        await messages.send_club_workout(bot, cb.from_user.id, n, is_rewatch=True)
    else:
        await messages.send_workout(bot, cb.from_user.id, n, funnel.slots_of(u["slot"])[0], is_rewatch=True)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery) -> None:
    await cb.answer()


# ─────────────────────────────── «Как ощущения?» ───────────────────────────────

@router.callback_query(F.data.startswith("fb:"))
async def feedback_mood(cb: CallbackQuery) -> None:
    _, day, mood = cb.data.split(":")
    if mood not in texts.MOODS:
        return await cb.answer()
    await cb.answer()
    await db.add_feedback(cb.from_user.id, int(day), mood)
    await db.set_field(cb.from_user.id, "awaiting_feedback_day", int(day))
    try:
        await cb.message.edit_text(f"{cb.message.html_text}\n\n<b>→ {texts.MOODS[mood]}</b>")
    except Exception:
        pass
    await cb.message.answer(texts.CHECKIN_ASK_TEXT, reply_markup=keyboards.checkin_skip())


@router.callback_query(F.data == "fbskip")
async def feedback_skip(cb: CallbackQuery) -> None:
    await cb.answer()
    await db.set_field(cb.from_user.id, "awaiting_feedback_day", None)
    await cb.message.edit_text(texts.CHECKIN_SKIPPED)


# ─────────────────────────────── настройки ───────────────────────────────

async def _settings_text(u) -> str:
    slot = texts.SLOTS.get(u["slot"], "—")
    tz = texts.TIMEZONES.get(u["tz"], u["tz"])
    subscribed = texts.SUBSCRIBED_OFF if u["unsubscribed"] else texts.SUBSCRIBED_ON
    return texts.SETTINGS.format(slot=slot, tz=tz, subscribed=subscribed)


@router.message(Command("testme"))
async def testme_not_admin(message: Message) -> None:
    """/testme у админа ловит admin.router раньше; сюда попадают все остальные."""
    await message.answer(
        "Эта команда — только для админа бота. Ваш id: "
        f"<code>{message.from_user.id}</code>.\n\nЧтобы проверять бота с этого аккаунта, "
        "добавьте этот id в Render → sister-bot → Environment → <code>ADMIN_IDS</code> "
        "(через запятую), Save — и через пару минут /testme заработает.")


@router.message(Command("settings"))
async def settings(message: Message) -> None:
    u = await db.get_user(message.from_user.id)
    if u is None or u["started_at"] is None:
        await message.answer(texts.NOT_STARTED)
        return
    await message.answer(await _settings_text(u), reply_markup=keyboards.settings(u["unsubscribed"]))


@router.callback_query(F.data == "set:slot")
async def settings_slot(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(texts.Q_SLOT, reply_markup=keyboards.choices("q:slot", texts.SLOTS))


@router.callback_query(F.data == "set:tz")
async def settings_tz(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(texts.TZ_CHOOSE, reply_markup=keyboards.choices("tz", texts.TIMEZONES))


@router.callback_query(F.data.startswith("tz:"))
async def set_tz(cb: CallbackQuery) -> None:
    tz = cb.data[3:]
    if tz not in texts.TIMEZONES:
        return await cb.answer()
    await db.set_field(cb.from_user.id, "tz", tz)
    await cb.answer(texts.SAVED)
    await cb.message.edit_text(f"{texts.TZ_CHOOSE}\n\n<b>→ {texts.TIMEZONES[tz]}</b>")


@router.message(Command("stop"))
@router.callback_query(F.data == "unsub")
async def unsubscribe(event: Message | CallbackQuery) -> None:
    await db.set_field(event.from_user.id, "unsubscribed", True)
    if isinstance(event, CallbackQuery):
        await event.answer()
        event = event.message
    await event.answer(texts.UNSUBSCRIBED)


@router.callback_query(F.data == "resub")
async def resubscribe(cb: CallbackQuery) -> None:
    await db.set_field(cb.from_user.id, "unsubscribed", False)
    await cb.answer()
    await cb.message.answer(texts.RESUBSCRIBED)


# ─────────────────────────────── клуб и оплата ───────────────────────────────

@router.message(Command("club"))
@router.callback_query(F.data == "club")
async def club(event: Message | CallbackQuery, bot: Bot) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer()
    fu = event.from_user
    u = await db.get_user(fu.id) or await db.upsert_user(fu.id, fu.username, fu.first_name, None)
    if u["paid_at"]:
        # Купившей — свежая ссылка (старая могла истечь или потеряться).
        await payments.send_club_link(bot, u["tg_id"], await db.paid_plan(u["tg_id"]), again=True)
        return
    await messages.send_club_offer(bot, u)


@router.callback_query(F.data.startswith("stars:"))
async def pay_stars(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    code = cb.data.split(":", 1)[1]
    plan = config.PLANS.get(code)
    if plan is None or plan["stars"] <= 0:
        return
    await bot.send_invoice(
        chat_id=cb.from_user.id, title=texts.STARS_TITLE,
        description=texts.STARS_DESCRIPTION.format(plan=texts.PLAN_NAMES[code]),
        payload=f"club:{code}", currency="XTR",
        prices=[LabeledPrice(label=texts.STARS_TITLE, amount=plan["stars"])])


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery) -> None:
    await q.answer(ok=q.invoice_payload.removeprefix("club:") in config.PLANS)


@router.message(F.successful_payment)
async def stars_paid(message: Message, bot: Bot) -> None:
    sp = message.successful_payment
    plan = sp.invoice_payload.removeprefix("club:")
    pid = await db.add_payment(message.from_user.id, "stars", "stars:" + sp.telegram_payment_charge_id,
                               sp.total_amount, sp.currency, "paid",
                               {"payload": sp.invoice_payload, "plan": plan})
    if pid is not None:
        await payments.grant_access(bot, message.from_user.id, plan)


@router.callback_query(F.data == "ipaid")
async def i_paid(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(texts.I_PAID_WHICH, reply_markup=keyboards.which_plan("ipaid"))


@router.callback_query(F.data.startswith("ipaid:"))
async def i_paid_plan(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    code = cb.data.split(":", 1)[1]
    if code not in config.PLANS:
        return
    fu = cb.from_user
    u = await db.get_user(fu.id) or await db.upsert_user(fu.id, fu.username, fu.first_name, None)
    await cb.message.answer(await payments.manual_claim(bot, u, code))


# ─────────────────────────────── свободный текст ───────────────────────────────

@router.message(F.text & ~F.text.startswith("/"))
async def free_text(message: Message, bot: Bot) -> None:
    u = await db.get_user(message.from_user.id)
    if u is None or u["started_at"] is None:
        await message.answer(texts.NOT_STARTED)
        return
    if await health.handle_text(message, bot):
        return
    day = u["awaiting_feedback_day"]
    if day is None:
        await message.answer(texts.DEFAULT_REPLY)
        return
    fid = await db.add_feedback_text(u["tg_id"], day, message.text[:2000])
    await db.set_field(u["tg_id"], "awaiting_feedback_day", None)
    await message.answer(texts.CHECKIN_THANKS)
    await messages.notify_admins(
        bot, f"💬 Новый отзыв (день {day}) от {messages.name_of(u)}:\n\n«{html.escape(message.text[:2000])}»",
        keyboards.moderate(fid))
