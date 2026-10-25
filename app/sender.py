"""Безопасная отправка: не больше SEND_RATE сообщений в секунду, повтор при
«слишком часто», отметка участниц, которые заблокировали бота."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramNetworkError, TelegramRetryAfter)

from . import config, db

log = logging.getLogger(__name__)

OK, BLOCKED, FAILED = "ok", "blocked", "failed"


class RateLimiter:
    def __init__(self, per_second: int) -> None:
        self.interval = 1 / max(per_second, 1)
        self.lock = asyncio.Lock()
        self.next_at = 0.0

    async def wait(self) -> None:
        async with self.lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self.next_at > now:
                await asyncio.sleep(self.next_at - now)
                now = self.next_at
            self.next_at = now + self.interval


limiter = RateLimiter(config.SEND_RATE)


async def call(tg_id: int, make: Callable[[], Awaitable]) -> str:
    """Выполнить один запрос к Telegram для участницы tg_id (с лимитом и повторами)."""
    for attempt in range(3):
        await limiter.wait()
        try:
            await make()
            return OK
        except TelegramRetryAfter as e:
            log.warning("Telegram просит подождать %s с", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            await db.set_field(tg_id, "blocked", True)
            return BLOCKED
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "chat not found" in msg or "user is deactivated" in msg:
                await db.set_field(tg_id, "blocked", True)
                return BLOCKED
            log.error("Ошибка отправки %s: %s", tg_id, e)
            return FAILED
        except TelegramNetworkError as e:
            log.warning("Сеть, попытка %s, %s: %s", attempt + 1, tg_id, e)
            await asyncio.sleep(2)
        except Exception:
            log.exception("Неожиданная ошибка отправки %s", tg_id)
            return FAILED
    return FAILED
