"""Quest commands preserve source messages and participate in native dispatch."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Update, User

from msu_hub_bot.commands.quest import Quest
from msu_hub_bot.commands.quest_view import QuestCallback
from msu_hub_bot.providers.quest import QuestError
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.state import ReleasableEventIsolation, StateContextMiddleware, TopicFSMContextMiddleware
from telegram_helpers import make_bot, make_message
from test_dispatch_contract import Selection, router


@pytest.mark.parametrize("arguments,story_id", [([], "demo"), (["demo"], "demo"), (["synthetic-story-id"], "synthetic-story-id")])
async def test_command_passes_story_id_and_preserves_source(arguments, story_id):
    bot = make_bot()
    message = make_message(bot)
    service = AsyncMock()
    try:
        await Quest.process(message, MetaInfo(message, arguments=arguments), service)
        service.start.assert_awaited_once_with(message, story_id)
        assert bot.session.methods == []
    finally:
        await bot.session.close()


@pytest.mark.parametrize("error", [RuntimeError("PRIVATE_SYNTHETIC_DETAIL"), QuestError("PRIVATE_SYNTHETIC_DETAIL")])
async def test_failure_uses_bounded_public_reply(error):
    bot = make_bot()
    message = make_message(bot)
    service = AsyncMock()
    service.start.side_effect = error
    try:
        await Quest.process(message, MetaInfo(message), service)
        assert len(bot.session.methods) == 1
        assert "Не удалось открыть" in bot.session.methods[0].text
        assert "PRIVATE_SYNTHETIC_DETAIL" not in bot.session.methods[0].text
    finally:
        await bot.session.close()


async def test_multiple_arguments_are_not_silently_ignored():
    bot = make_bot()
    message = make_message(bot)
    service = AsyncMock()
    try:
        await Quest.process(message, MetaInfo(message, arguments=["demo", "extra"]), service)
        service.start.assert_not_called()
        assert "один квест" in bot.session.methods[0].text
    finally:
        await bot.session.close()


@pytest.mark.parametrize(
    "text,expected", [("/quest", "Quest.process"), ("/QUEST@CONTRACT_BOT demo", "Quest.process"), ("/quest@other_bot", None)]
)
async def test_command_selects_native_route(text, expected):
    bot = make_bot()
    bot._me = User(id=bot.id, is_bot=True, first_name="Bot", username="contract_bot")
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.message.middleware(Selection())
    dispatcher.include_router(router())
    try:
        result = await dispatcher.feed_update(bot, Update(update_id=1, message=make_message(bot, text=text)))
        if expected is None:
            assert result is UNHANDLED
        else:
            handler, meta = result
            assert handler.flags["handler_key"] == expected
            assert handler.flags["fsm_release"] is True
            assert meta.command.lower() == "quest"
    finally:
        await dispatcher.fsm.close()
        await bot.session.close()


@pytest.mark.parametrize("action,value", [("vote", "0"), ("finish", ""), ("page", "1")])
async def test_quest_buttons_work_during_another_conversation_without_losing_draft(action, value):
    bot = make_bot()
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(fsm)
    dispatcher.callback_query.middleware(Selection())
    dispatcher.include_router(router())
    message = make_message(bot, message_thread_id=17, is_topic_message=True)
    state = fsm.resolve_context(bot, message.chat.id, 42, thread_id=17)
    await state.set_state("ProgStates:stdin")
    await state.set_data({"draft": "keep this"})
    query = CallbackQuery(
        id="synthetic",
        chat_instance="synthetic",
        from_user=message.from_user,
        message=message,
        data=QuestCallback(game_id="abcdef123456", scene_version=0, action=action, value=value).pack(),
    )
    try:
        handler, _ = await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, callback_query=query)))
        assert handler.flags["handler_key"] == "Quest.callback"
        assert await state.get_state() == "ProgStates:stdin"
        assert await state.get_data() == {"draft": "keep this"}
    finally:
        await fsm.close()
        await dispatcher.fsm.close()
        await bot.session.close()
