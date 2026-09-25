"""The art command delegates lifecycle and keeps Telegram failures bounded."""

from unittest.mock import AsyncMock

import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, InaccessibleMessage
from aiogram.utils.formatting import Bold, Text, TextLink

from msu_hub_bot.commands.art import Art, ArtCallback
from telegram_helpers import make_bot, make_message


async def test_start_delegates_to_art_quiz_and_preserves_the_invocation():
    message = make_message(text="/art")
    service = AsyncMock()
    service.start.return_value = make_message(message_id=2)
    assert await Art.process(message, service) == service.start.return_value
    service.start.assert_awaited_once_with("art", message)


@pytest.mark.parametrize("choice", ["0", "5", "finish", "page_2"])
async def test_callback_passes_round_and_choice_to_shared_quiz(choice):
    message = make_message()
    query = CallbackQuery(id="test", chat_instance="test", from_user=message.from_user, message=message)
    service = AsyncMock()
    service.callback.return_value = True
    data = ArtCallback(round="a" * 12, choice=choice)
    assert await Art.process_cb(query, data, service) is True
    service.callback.assert_awaited_once_with("art", query, data.round, choice)
    assert Art.callback_data.unpack(data.pack()) == data
    assert len(data.pack().encode()) <= 64


@pytest.mark.parametrize("inaccessible", [False, True])
async def test_callback_without_accessible_message_acknowledges_without_service_call(inaccessible):
    bot = make_bot()
    message = make_message(bot)
    query = CallbackQuery(
        id="test",
        chat_instance="test",
        from_user=message.from_user,
        message=InaccessibleMessage(chat=message.chat, message_id=1, date=0) if inaccessible else None,
    ).as_(bot)
    service = AsyncMock()
    try:
        assert await Art.process_cb(query, ArtCallback(round="a" * 12, choice="0"), service) is True
        service.callback.assert_not_awaited()
        assert len(bot.session.methods) == 1
        method = bot.session.methods[0]
        assert isinstance(method, AnswerCallbackQuery)
        assert method.text == "Этот раунд недоступен."
    finally:
        await bot.session.close()


async def test_daily_ranking_preserves_names_usernames_and_entities():
    bot = make_bot()
    message = make_message(bot, text="/art_top")
    service = AsyncMock()
    body = Text(Bold("🎨 Рейтинг за сегодня"), "\n1. ", TextLink("Аня <&>", url="tg://user?id=11"), " (@anya) — 3")
    service.ranking.return_value = body
    try:
        await Art.top(message, service)
        service.ranking.assert_awaited_once_with("art", message.chat.id)
        assert len(bot.session.methods) == 1
        method = bot.session.methods[0]
        assert isinstance(method, SendMessage)
        assert (method.text, method.entities) == body.render()
        assert method.parse_mode is None
        assert method.reply_parameters.message_id == message.message_id
    finally:
        await bot.session.close()


@pytest.mark.parametrize("failure", [RuntimeError("PRIVATE_SYNTHETIC_DETAIL"), TimeoutError()])
async def test_ranking_failure_replies_without_leaking_internal_details(failure):
    bot = make_bot()
    service = AsyncMock()
    service.ranking.side_effect = failure
    try:
        await Art.top(make_message(bot), service)
        assert len(bot.session.methods) == 1
        method = bot.session.methods[0]
        assert isinstance(method, SendMessage)
        assert method.text == "Рейтинг сейчас недоступен."
    finally:
        await bot.session.close()
