"""Точка входа.

На Render:   python -m app.main            (вебхук Telegram + /tick + /tribute)
Локально:    python -m app.main --polling  (без вебхука, для проверки)
"""
from __future__ import annotations

import asyncio
import hmac
import html
import logging
import os
import sys
import time
import traceback

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from . import config, db, messages, payments, tick, tribute_router
from .handlers import admin, channel, club_channels, health, user

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sisters-bot")
WEBHOOK_PATH = "/webhook"


def build() -> tuple[Bot, Dispatcher]:
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(club_channels.router)   # пересланный пост из канала — раньше шагов админки
    dp.include_router(admin.router)      # админка первой: её шаги (/upload …) важнее «свободного текста»
    dp.include_router(channel.router)
    dp.include_router(health.router)
    dp.include_router(user.router)
    dp.errors.register(on_error)
    return bot, dp


_last_alert: dict[str, float] = {}


async def on_error(event: ErrorEvent, bot: Bot) -> bool:
    """Ошибка при ответе сестре — сестра «застревает». Сразу пишем админам
    (одна и та же ошибка — не чаще раза в 30 минут)."""
    exc = event.exception
    log.exception("Ошибка в обработчике", exc_info=exc)
    upd = event.update
    who, what = "", ""
    if upd.message:
        who, what = upd.message.from_user, f"сообщение «{(upd.message.text or '')[:50]}»"
    elif upd.callback_query:
        who, what = upd.callback_query.from_user, f"кнопка «{upd.callback_query.data}»"
    where = "".join(traceback.format_tb(exc.__traceback__)[-1:]).strip().splitlines()[0][:200]
    key = f"{type(exc).__name__}:{where}"
    now = time.monotonic()
    if now - _last_alert.get(key, -1e9) < 1800:
        return True
    _last_alert[key] = now
    person = (f"{html.escape(who.first_name or '')} (id {who.id})" if who else "—")
    await messages.notify_admins(
        bot, "⚠️ <b>Ошибка в боте</b> — сестра могла застрять.\n"
             f"Кто: {person}\nЧто нажала: {html.escape(what) or '—'}\n"
             f"Ошибка: <code>{html.escape(type(exc).__name__)}: {html.escape(str(exc)[:300])}</code>\n"
             f"<code>{html.escape(where)}</code>\n\nПерешлите это сообщение Claude — починю.")
    return True


async def on_startup(bot: Bot, dp: Dispatcher, webhook: bool) -> None:
    await db.connect()
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать"),
        BotCommand(command="settings", description="Время, часовой пояс, рассылка"),
        BotCommand(command="club", description="Платный клуб"),
    ])
    if webhook:
        await bot.set_webhook(config.BASE_URL + WEBHOOK_PATH, secret_token=config.WEBHOOK_SECRET or None,
                              allowed_updates=dp.resolve_used_update_types())
        log.info("Вебхук: %s", config.BASE_URL + WEBHOOK_PATH)


def _key_ok(given: str) -> bool:
    return bool(config.TICK_KEY) and hmac.compare_digest(given.encode(), config.TICK_KEY.encode())


def make_app(bot: Bot, dp: Dispatcher) -> web.Application:
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def status(_: web.Request) -> web.Response:
        """Для сторожа (tg-promo-agent/bot_watchdog.py): работает ли Telegram, база,
        какая версия кода запущена. Без секретов — только «ок / что сломано»."""
        out: dict = {"commit": os.environ.get("RENDER_GIT_COMMIT", "")}
        try:
            me = await bot.get_me()
            out["telegram"] = "ok"
            out["username"] = me.username
            info = await bot.get_webhook_info()
            out["pending"] = info.pending_update_count
            out["webhook_set"] = bool(info.url)
            if info.last_error_date:
                out["webhook_error_at"] = int(info.last_error_date.timestamp())
                out["webhook_error"] = (info.last_error_message or "")[:200]
        except Exception as e:                      # чаще всего — неверный BOT_TOKEN
            out["telegram"] = f"{type(e).__name__}: {str(e)[:200]}"
        try:
            await db.ping()
            out["db"] = "ok"
        except Exception as e:
            out["db"] = f"{type(e).__name__}: {str(e)[:200]}"
        return web.json_response(out)

    background: set[asyncio.Task] = set()

    async def tick_in_background() -> None:
        try:
            await tick.run(bot)
        except Exception:
            log.exception("Тик упал")

    async def tick_handler(request: web.Request) -> web.Response:
        if not _key_ok(request.query.get("key", "")):
            return web.json_response({"error": "forbidden"}, status=403)
        if request.query.get("bg"):
            # Для внешних будильников (cron-job.org): короткий ответ сразу,
            # рассылка идёт в фоне — без таймаутов и больших ответов.
            task = asyncio.create_task(tick_in_background())
            background.add(task)
            task.add_done_callback(background.discard)
            return web.Response(text="ok")
        try:
            result = await tick.run(bot)
        except Exception:
            log.exception("Тик упал")
            return web.json_response({"error": "tick failed"}, status=500)
        return web.json_response(result)

    async def tribute_handler(request: web.Request) -> web.Response:
        body = await request.read()
        signature = request.headers.get("trbt-signature", "")
        if not payments.tribute_signature_ok(body, signature):
            # Причина без секретов: задан ли ключ, пришла ли подпись и какого вида.
            reason = ("TRIBUTE_API_KEY не задан в Render" if not config.TRIBUTE_API_KEY
                      else "нет заголовка trbt-signature" if not signature
                      else "подпись не совпала — ключ в Render не тот, что в Tribute")
            log.warning("Tribute 401: %s. Длина ключа %s, длина подписи %s, заголовки: %s",
                        reason, len(config.TRIBUTE_API_KEY), len(signature),
                        ", ".join(h for h in request.headers if h.lower() not in ("authorization", "cookie")))
            return web.json_response({"error": "bad signature", "reason": reason}, status=401)
        try:
            return web.json_response(await tribute_router.handle(
                bot, body, request.headers.get("trbt-signature", "")))
        except Exception:
            log.exception("Ошибка обработки Tribute")
            return web.json_response({"error": "internal"}, status=500)

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    app.router.add_route("*", "/tick", tick_handler)
    app.router.add_post("/tribute", tribute_handler)
    SimpleRequestHandler(dispatcher=dp, bot=bot,
                         secret_token=config.WEBHOOK_SECRET or None).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    async def startup(_: web.Application) -> None:
        await on_startup(bot, dp, webhook=True)

    async def cleanup(_: web.Application) -> None:
        await db.close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def check_config(webhook: bool) -> None:
    missing = [n for n in ("BOT_TOKEN", "DATABASE_URL") if not getattr(config, n)]
    if webhook:
        missing += [n for n in ("BASE_URL", "TICK_KEY", "WEBHOOK_SECRET") if not getattr(config, n)]
    if missing:
        sys.exit("Не заданы переменные окружения: " + ", ".join(missing))
    if not config.ADMIN_IDS:
        log.warning("ADMIN_IDS не задан — админка и уведомления об оплатах работать не будут")


async def polling() -> None:
    bot, dp = build()
    await on_startup(bot, dp, webhook=False)
    await bot.delete_webhook()
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()


def main() -> None:
    if "--polling" in sys.argv:
        check_config(webhook=False)
        asyncio.run(polling())
        return
    check_config(webhook=True)
    bot, dp = build()
    web.run_app(make_app(bot, dp), host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
