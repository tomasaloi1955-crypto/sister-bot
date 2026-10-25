"""Оплата клуба: Tribute (вебхук), Telegram Stars, ручная проверка «Я оплатила».

Тарифа два (config.PLANS): 3x — 3 раза в неделю, 6x — каждый день. Тариф
оплаты пишется в payments.raw->>'plan'; по нему выбирается канал клуба.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from . import config, db, keyboards, messages, texts

log = logging.getLogger(__name__)


def plan_name(plan: str | None) -> str:
    return texts.PLAN_NAMES.get(plan or "", texts.PLAN_NAMES[config.DEFAULT_PLAN])


async def send_club_link(bot: Bot, tg_id: int, plan: str | None, again: bool = False) -> str | None:
    """Новая одноразовая ссылка в канал тарифа (действует 7 дней). None — не вышло."""
    chat = config.PLANS.get(plan or "", config.PLANS[config.DEFAULT_PLAN])["chat"]
    link = None
    if chat:
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


async def grant_access(bot: Bot, tg_id: int, plan: str | None) -> None:
    """Покупка подтверждена: отмечаем, шлём одноразовую ссылку в клуб.
    После отметки paid_at продающие рассылки этой участнице прекращаются."""
    await db.mark_paid(tg_id)
    link = await send_club_link(bot, tg_id, plan)
    u = await db.get_user(tg_id)
    who = messages.name_of(u) + (f" @{u['username']}" if u and u["username"] else "")
    note = "" if plan in config.PLANS else \
        "\n⚠️ Тариф не опознан — впиши id товаров в TRIBUTE_PRODUCT_IDS_3X / _6X."
    await messages.notify_admins(
        bot, f"💰 Покупка клуба: {who} (id {tg_id}), тариф: {plan_name(plan)}" + note
             + ("" if link else "\n⚠️ Ссылку создать не удалось — выдай доступ вручную."))


# ─────────────────────────────── Tribute ───────────────────────────────

def tribute_signature_ok(body: bytes, signature: str) -> bool:
    """Tribute подписывает тело HMAC-SHA256 API-ключом, заголовок trbt-signature
    (так же, как в школьном сайте — web.py)."""
    if not config.TRIBUTE_API_KEY or not signature:
        return False
    expected = hmac.new(config.TRIBUTE_API_KEY.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


PAID_EVENTS = ("new_digital_product", "new_subscription", "renewed_subscription")


def tribute_plan(ids: set[str]) -> str | None:
    for code, plan in config.PLANS.items():
        if ids & plan["products"]:
            return code
    return None


async def handle_tribute(bot: Bot, body: bytes) -> dict:
    event = json.loads(body or b"{}")
    name = event.get("name", "")
    p = event.get("payload") or {}
    if name not in PAID_EVENTS:
        return {"ok": True, "ignored": name}
    ids = {str(p[k]) for k in ("product_id", "subscription_id", "period_id") if p.get(k) is not None}
    plan = tribute_plan(ids)
    known = set().union(*(pl["products"] for pl in config.PLANS.values()))
    if known and plan is None:
        log.info("Tribute: чужой товар %s — пропускаем", ids)
        return {"ok": True, "ignored": "другой товар"}
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
    log.info("Tribute: %s %s от %s, товар %s, тариф %s", name, external, tg_id, ids, plan)
    if name == "renewed_subscription":
        # Продление: доступ у неё уже есть, новую ссылку не шлём.
        await messages.say(bot, tg_id, texts.RENEWED.format(plan=plan_name(plan)))
        await messages.notify_admins(bot, f"🔁 Продление клуба: id {tg_id}, тариф: {plan_name(plan)}")
        return {"ok": True, "renewed": True}
    await grant_access(bot, tg_id, plan)
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
