"""Просроченное нажатие кнопки не должно ломать сценарий.

Render free засыпает; пока сервис просыпается, Telegram «просрочивает» нажатие
кнопки (ответить на callback можно ~15 секунд). Тогда answerCallbackQuery падает
с «query is too old…», обработчик обрывается — и сестра застревает в анкете
(так было 07.11 с «q:slot:evening»). Этот слой глотает только эту ошибку и только
для answerCallbackQuery: ответ на нажатие — лишь «часики» на кнопке, сам сценарий
(сохранить выбор, задать следующий вопрос) продолжается.
"""
from __future__ import annotations

import logging

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery

log = logging.getLogger(__name__)

_STALE = ("query is too old", "query id is invalid")


class IgnoreStaleCallback(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as e:
            if isinstance(method, AnswerCallbackQuery) and any(s in str(e).lower() for s in _STALE):
                log.warning("Нажатие кнопки просрочено (сервис просыпался) — продолжаем без ответа")
                return True
            raise
