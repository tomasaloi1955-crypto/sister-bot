"""Защита от второго экземпляра бота.

У бота один токен, а Telegram шлёт сообщения только на один адрес (вебхук).
Если где-то запустили старую копию сервиса (например, на старом кабинете Render),
она при запуске забрала бы вебхук себе — и сёстры писали бы в «не тот» бот.
Поэтому вебхук берём, только если он пустой, уже наш, или тот адрес не отвечает.
Переезд на новый адрес нарочно: переменная TAKE_WEBHOOK=1 на новом сервисе.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger(__name__)


def decide(current: str, ours: str, force: bool, other_alive: bool | None) -> str:
    """take — ставим наш вебхук; keep — не трогаем (там другой живой бот);
    check — нужно проверить, жив ли тот адрес (other_alive=None)."""
    if force or not current or current == ours:
        return "take"
    if urlsplit(current).netloc == urlsplit(ours).netloc:
        return "take"                       # тот же сервис, другой путь
    if other_alive is None:
        return "check"
    return "keep" if other_alive else "take"


async def is_alive(url: str, timeout: float = 60) -> bool:
    """Работает ли наш бот по адресу вебхука: /health отвечает ровно «ok»
    (страница «сервис на паузе» или ошибка Render — не считается).
    Бесплатный Render просыпается до минуты."""
    p = urlsplit(url)
    base = f"{p.scheme}://{p.netloc}/health"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.get(base) as r:
                return r.status == 200 and (await r.text()).strip() == "ok"
    except Exception as e:
        log.info("Адрес вебхука %s не отвечает: %s", base, e)
        return False
