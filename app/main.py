"""Точка входа.

На Render:   python -m app.main            (вебхук Telegram + /tick + /tribute)
Локально:    python -m app.main --polling  (без вебхука, для проверки)
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from . import config, db, payments, tick
from .handlers import admin, user

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sisters-bot")
WEBHOOK_PATH = "/webhook"


def build() -> tuple[Bot, Dispatcher]:
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin.router)      # админка первой: её шаги (/upload …) важнее «свободного текста»
    dp.include_router(user.router)
    return bot, dp


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

    async def tick_handler(request: web.Request) -> web.Response:
        if not _key_ok(request.query.get("key", "")):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            result = await tick.run(bot)
        except Exception:
            log.exception("Тик упал")
            return web.json_response({"error": "tick failed"}, status=500)
        return web.json_response(result)

    async def tribute_handler(request: web.Request) -> web.Response:
        body = await request.read()
        if not payments.tribute_signature_ok(body, request.headers.get("trbt-signature", "")):
            return web.json_response({"error": "bad signature"}, status=401)
        try:
            return web.json_response(await payments.handle_tribute(bot, body))
        except Exception:
            log.exception("Ошибка обработки Tribute")
            return web.json_response({"error": "internal"}, status=500)

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
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
