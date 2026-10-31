"""Оплата клуба: Tribute (вебхук), Telegram Stars, ручная проверка «Я оплатила».

Тарифа два (config.PLANS): 3x — 3 раза в неделю, 6x — каждый день. Тариф
оплаты пишется в payments.raw->>'plan'; по нему выбирается канал клуба.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from . import config, db, keyboards, messages, texts

log = logging.getLogger(__name__)


def plan_name(plan: str | None) -> str:
    return texts.PLAN_NAMES.get(plan or "", texts.PLAN_NAMES[config.DEFAULT_PLAN])


async def send_club_link(bot: Bot, tg_id: int, plan: str | None, again: bool = False,
                         via_tribute: bool = False) -> str | None:
    """Новая одноразовая ссылка в канал тарифа (действует 7 дней). None — не вышло.

    Канал тарифа не задан (CLUB_CHAT_ID пуст) — значит, в канал впускает сам
    Tribute (платный канал): ссылку она получает от бота Tribute."""
    chat = config.PLANS.get(plan or "", config.PLANS[config.DEFAULT_PLAN])["chat"]
    if not chat:
        text = (texts.CLUB_VIA_TRIBUTE if again
                else texts.ACCESS_VIA_TRIBUTE if via_tribute else texts.ACCESS_GRANTED_NO_LINK)
        await messages.say(bot, tg_id, text.format(plan=plan_name(plan)))
        return None
    link = None
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=int(chat), name=f"club {plan} {tg_id}"[:32], member_limit=1,
            expire_date=datetime.now(timezone.utc) + timedelta(days=7))
        link = invite.invite_link
    except Exception:
        log.exception("Не удалось создать ссылку в клуб (бот админ в клубе? CLUB_CHAT_ID верный?)")
    if link:
        text = texts.CLUB_LINK_AGAIN if again else texts.ACCESS_GRANTED
        await messages.say(bot, tg_id, text.format(link=link, plan=plan_name(plan)))
    else:
        await messages.say(bot, tg_id, texts.ACCESS_GRANTED_NO_LINK)
    return link


async def grant_access(bot: Bot, tg_id: int, plan: str | None, via_tribute: bool = False) -> None:
    """Покупка подтверждена: отмечаем, шлём одноразовую ссылку в клуб.
    После отметки paid_at продающие рассылки этой участнице прекращаются."""
    await db.mark_paid(tg_id)
    link = await send_club_link(bot, tg_id, plan, via_tribute=via_tribute)
    tribute_lets_in = via_tribute and not config.PLANS.get(plan or "", config.PLANS[config.DEFAULT_PLAN])["chat"]
    u = await db.get_user(tg_id)
    who = messages.name_of(u) + (f" @{u['username']}" if u and u["username"] else "")
    note = "" if plan in config.PLANS else "\n⚠️ Тариф не опознан."
    await messages.notify_admins(
        bot, f"💰 Покупка клуба: {who} (id {tg_id}), тариф: {plan_name(plan)}" + note
             + ("" if link or tribute_lets_in else "\n⚠️ Ссылки в канал нет — добавь её в канал вручную."))


async def mark_club_member(bot: Bot, tg_id: int, plan: str, chat_id: int) -> None:
    """Сестра в канале клуба (её впустил Tribute) — значит, купила. Отмечаем
    один раз, без ссылок: доступ у неё уже есть."""
    pid = await db.add_payment(tg_id, "channel", f"channel:{chat_id}:{tg_id}", None, None, "paid",
                               {"plan": plan, "chat_id": chat_id})
    first = await db.mark_paid(tg_id)
    if pid is None and not first:
        return
    u = await db.get_user(tg_id)
    who = messages.name_of(u) + (f" @{u['username']}" if u and u["username"] else "")
    await messages.notify_admins(bot, f"💰 В канале клуба новая участница: {who} (id {tg_id}), "
                                      f"тариф: {plan_name(plan)}. Продающие сообщения ей больше не придут.")


async def club_plan_by_membership(bot: Bot, tg_id: int) -> str | None:
    """Запасная проверка перед продажей: есть ли она в канале клуба."""
    for ch in await db.club_channels():
        if ch["plan"] not in config.PLANS:
            continue
        try:
            m = await bot.get_chat_member(ch["chat_id"], tg_id)
        except Exception:
            continue                                   # нет в канале или бот уже не админ
        if m.status in ("member", "restricted") and getattr(m, "is_member", True):
            await mark_club_member(bot, tg_id, ch["plan"], ch["chat_id"])
            return ch["plan"]
    return None


# ─────────────────────────────── Tribute ───────────────────────────────

def tribute_signature_ok(body: bytes, signature: str) -> bool:
    """Tribute подписывает тело HMAC-SHA256 API-ключом, заголовок trbt-signature
    (так же, как в школьном сайте — web.py)."""
    if not config.TRIBUTE_API_KEY or not signature:
        return False
    expected = hmac.new(config.TRIBUTE_API_KEY.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


PAID_EVENTS = ("new_digital_product", "new_subscription", "renewed_subscription")


async def handle_club(bot: Bot, event: dict, plan: str) -> dict:
    """Уведомление Tribute по товару клуба (диспетчер — tribute_router.py)."""
    name = event.get("name", "")
    p = event.get("payload") or {}
    if name not in PAID_EVENTS:
        return {"ok": True, "ignored": name}
    tg_id = p.get("telegram_user_id")
    if not tg_id:
        log.warning("Tribute: нет telegram_user_id в %s", p)
        return {"ok": True, "ignored": "нет telegram_user_id"}
    tg_id = int(tg_id)
    external = str(p.get("purchase_id") or p.get("transaction_id")
                   or f"{name}:{tg_id}:{event.get('created_at', '')}")
    pid = await db.add_payment(tg_id, "tribute", "tribute:" + external, p.get("amount") or p.get("price"),
                               p.get("currency"), "paid", {**event, "plan": plan})
    if pid is None:
        return {"ok": True, "duplicate": True}
    log.info("Tribute: клуб %s %s от %s, тариф %s", name, external, tg_id, plan)
    if name == "renewed_subscription":
        # Продление: доступ у неё уже есть, новую ссылку не шлём.
        await messages.say(bot, tg_id, texts.RENEWED.format(plan=plan_name(plan)))
        await messages.notify_admins(bot, f"🔁 Продление клуба: id {tg_id}, тариф: {plan_name(plan)}")
        return {"ok": True, "renewed": True}
    await grant_access(bot, tg_id, plan, via_tribute=True)
    return {"ok": True}


# ─────────────────────────────── «Я оплатила» ───────────────────────────────

async def manual_claim(bot: Bot, u, plan: str) -> str:
    """«Я оплатила» + выбранный тариф — заявка админу. Вернёт текст ответа участнице."""
    if u["paid_at"]:
        return texts.PAID_ALREADY
    if await db.pending_claim(u["tg_id"]):
        return texts.I_PAID_ALREADY
    pid = await db.add_payment(u["tg_id"], "manual", None, None, None, "pending", {"plan": plan})
    who = messages.name_of(u) + (f" @{u['username']}" if u["username"] else "")
    await messages.notify_admins(
        bot, f"🧾 «Я оплатила»: {who} (id {u['tg_id']}), тариф {plan_name(plan)} — "
             f"{config.PLANS[plan]['price']}.\nПроверь поступление в Tribute/банке:",
        keyboards.payment_check(pid))
    return texts.I_PAID_REPLY
