"""Диспетчер вебхука Tribute.

У Tribute один адрес уведомлений на аккаунт, а товаров несколько (школа,
клуб, курсы…). Все уведомления приходят сюда, и бот по номеру товара решает:
  club3x / club6x — клуб тренировок, обрабатываем сами (payments.py);
  school          — пересылаем на сайт школы без изменений;
  url:https://…   — пересылаем на этот адрес без изменений;
  ignore          — ничего не делаем (доступ выдаёт сам Tribute).
Незнакомый товар уходит в школу — как было до диспетчера, чтобы школа ничего
не потеряла, — а админу приходит вопрос с кнопками «чей это товар?».

Пересылаем то же тело с той же подписью trbt-signature: получатель проверяет
её тем же API-ключом Tribute, его код менять не нужно. Не дошло — уведомление
лежит в tribute_outbox, и тик досылает его, пока не примут.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiohttp
from aiogram import Bot

from . import config, db, keyboards, messages, payments

log = logging.getLogger(__name__)

ROUTES = ("school", "club3x", "club6x", "ignore")
ROUTE_NAMES = {"school": "🏫 Школа", "club3x": "🌿 Клуб 3 раза в неделю",
               "club6x": "🔥 Клуб каждый день", "ignore": "🚫 Ничей (не пересылать)"}
RETRY_MINUTES = (1, 2, 5, 10, 30, 60)        # дальше — раз в час
ALERT_AFTER = 3                               # после стольких неудач — сообщение админу
_background: set[asyncio.Task] = set()


def product_keys(p: dict) -> list[str]:
    return [str(p[k]) for k in ("product_id", "subscription_id", "period_id") if p.get(k) is not None]


def env_routes() -> dict[str, str]:
    """Товары клуба, заданные переменными TRIBUTE_PRODUCT_IDS_3X / _6X."""
    return {key: f"club{code}" for code, plan in config.PLANS.items() for key in plan["products"]}


def decide(keys: list[str], from_env: dict[str, str], from_db: dict[str, str | None]) -> str | None:
    """Маршрут по номерам товара. None — товар незнакомый."""
    for key in keys:
        if key in from_env:
            return from_env[key]
    for key in keys:
        if from_db.get(key):
            return from_db[key]
    return None


def target_url(route: str) -> str | None:
    if route == "school":
        return config.SCHOOL_WEBHOOK_URL
    if route.startswith("url:"):
        return route[4:]
    return None


def route_name(route: str | None) -> str:
    if route and route.startswith("url:"):
        return "🔗 " + route[4:]
    return ROUTE_NAMES.get(route or "", "❔ не решено (идёт в школу)")


async def handle(bot: Bot, body: bytes, signature: str) -> dict:
    event = json.loads(body or b"{}")
    name = event.get("name", "")
    p = event.get("payload") or {}
    keys = product_keys(p)
    route = decide(keys, env_routes(), await db.product_routes(keys) if keys else {})
    if route is None:
        route = "school"
        if keys:
            info = f"{p.get('amount') or p.get('price') or '?'} {p.get('currency') or ''}".strip()
            if await db.remember_product(keys[0], info):
                await messages.notify_admins(
                    bot, f"🆕 Новый товар в Tribute: id {keys[0]}, событие {name}, сумма {info}.\n"
                         "Чей он? Пока не ответите — уведомления по нему идут в школу.\n"
                         f"Другой адрес: <code>/route {keys[0]} https://…</code>",
                    keyboards.product_route(keys[0]))
    log.info("Tribute: %s, товары %s → %s", name, keys, route)

    if route in ("club3x", "club6x"):
        return await payments.handle_club(bot, event, route[4:])
    url = target_url(route)
    if url is None:
        return {"ok": True, "ignored": route}
    oid = await db.outbox_add(url, body.decode("utf-8"), signature)
    task = asyncio.create_task(deliver(bot, oid))   # Tribute получает ответ сразу, пересылка — в фоне
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"ok": True, "forwarded": route}


async def replay_for_club(bot: Bot, key: str, route: str) -> int:
    """Товар только что отмечен как клуб: уведомления по нему, которые до этого
    ушли в школу, обрабатываем как клубные (сестра получает доступ, бот перестаёт
    ей продавать). Повторы отсекает payments.handle_club."""
    done = 0
    for item in await db.outbox_recent():
        event = json.loads(item["body"])
        if key in product_keys(event.get("payload") or {}):
            result = await payments.handle_club(bot, event, route[4:])
            done += 1 if result.get("ok") and not result.get("ignored") and not result.get("duplicate") else 0
    return done


async def deliver(bot: Bot, oid: int) -> bool:
    """Отправить одно уведомление из очереди. True — получатель принял (2xx)."""
    item = await db.outbox_get(oid)
    if item is None or item["delivered_at"] is not None:
        return True
    try:
        timeout = aiohttp.ClientTimeout(total=90)          # школа на Render может просыпаться ~минуту
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(item["target_url"], data=item["body"].encode("utf-8"),
                              headers={"Content-Type": "application/json",
                                       "trbt-signature": item["signature"]}) as r:
                if 200 <= r.status < 300:
                    await db.outbox_done(oid)
                    if item["attempts"] >= ALERT_AFTER:
                        await messages.notify_admins(bot, f"✅ Уведомление Tribute #{oid} всё-таки доставлено.")
                    return True
                error = f"HTTP {r.status}: {(await r.text())[:200]}"
    except Exception as e:                                  # сеть, таймаут, адрес не отвечает
        error = f"{type(e).__name__}: {e}"
    delay = RETRY_MINUTES[min(item["attempts"], len(RETRY_MINUTES) - 1)]
    attempts = await db.outbox_failed(oid, error, delay)
    log.warning("Tribute: пересылка #%s на %s не удалась (%s), попытка %s", oid, item["target_url"], error, attempts)
    if attempts == ALERT_AFTER:
        await messages.notify_admins(
            bot, f"⚠️ Не могу переслать уведомление об оплате #{oid} на {item['target_url']}: {error}\n"
                 "Буду пробовать дальше. Если это школа — проверьте, работает ли сайт.")
    return False


async def retry_due(bot: Bot) -> int:
    """Из тика: дослать всё, чему пришло время. Вернёт, сколько доставлено."""
    done = 0
    for item in await db.outbox_due():
        done += await deliver(bot, item["id"])
    return done
