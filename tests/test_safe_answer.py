"""Просроченное нажатие кнопки: answerCallbackQuery не обрывает обработчик."""
import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage

from app.safe_answer import IgnoreStaleCallback

STALE = "Bad Request: query is too old and response timeout expired or query ID is invalid"


def call(method, err):
    async def make_request(bot, m):
        raise TelegramBadRequest(method=m, message=err)
    return asyncio.run(IgnoreStaleCallback()(make_request, None, method))


def test_stale_callback_answer_is_swallowed():
    assert call(AnswerCallbackQuery(callback_query_id="1"), STALE) is True


def test_other_errors_still_raise():
    with pytest.raises(TelegramBadRequest):
        call(AnswerCallbackQuery(callback_query_id="1"), "Bad Request: message is too long")
    with pytest.raises(TelegramBadRequest):
        call(SendMessage(chat_id=1, text="x"), STALE)
