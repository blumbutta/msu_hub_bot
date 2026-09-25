"""Replay captured selection policies through native aiogram observers and FSM."""

import asyncio
import json
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram import BaseMiddleware, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InaccessibleMessage, Update, User

from msu_hub_bot.commands.chess import ChessCallback
from msu_hub_bot.commands.art import ArtCallback
from msu_hub_bot.commands.geoguess import GeoguessCallback
from msu_hub_bot.commands.reactions import ReactionCallback
from msu_hub_bot.telegram.filters import MetaCommand, SlashCommand
from msu_hub_bot.telegram.middlewares.settings import SettingsMiddleware
from msu_hub_bot.telegram.state import (
    ReleasableEventIsolation,
    SelectiveIsolationMiddleware,
    StateContextMiddleware,
    TopicFSMContextMiddleware,
)
from msu_hub_bot.routing import build_router
from msu_hub_bot.providers.wit import Wit
from msu_hub_bot.providers.wolfram import WolframAPI
from msu_hub_bot.settings import Settings
from msu_hub_bot.telemetry import Telemetry
from telegram_helpers import RecordingSession, make_bot, make_message
from telemetry_helpers import Capture, config

CONTRACT = json.loads((Path(__file__).parent / "fixtures/routing_contract.json").read_text())


class Selection(BaseMiddleware):
    async def __call__(self, handler, event, data):
        selected = data["handler"]
        if selected.flags["handler_key"] == "process_pokakats":
            return await handler(event, data)
        return selected, data.get("meta")


def router():
    return build_router(wit=Wit([]), wolfram=WolframAPI(""), config=Settings(owner_id=7, founder_ids=[8]))


def routes(root, event):
    return [handler for child in root.chain_tail for handler in child.observers[event].handlers]


def aliases(handler):
    for item in handler.filters:
        if isinstance(item.callback, MetaCommand):
            return list(item.callback.parser.commands)
        if isinstance(item.callback, SlashCommand):
            return list(item.callback.commands)
    return []


def is_added_route(handler):
    return aliases(handler) == ["py_stdin", "python_stdin"] or handler.flags["handler_key"] in {
        "Art.process",
        "Art.top",
        "Art.process_cb",
        "Chess.process",
        "Chess.top",
        "Chess.process_cb",
        "ChessPlay.process",
        "ChessPlay.callback",
        "ChessRating.process",
        "ChessRating.callback",
        "process_meme",
        "Reactions.process",
        "Reactions.process_cb",
        "Remind.process",
        "Remind.process_cb",
        "Feedback.process",
        "Feedback.process_cb",
        "process_app",
        "process_app_start",
        "process_intent",
    }


def test_every_route_preserves_order_and_aliases():
    root = router()
    counts = Counter(route["event"] for route in CONTRACT["routes"])
    for kind, count in counts.items():
        actual = routes(root, "error" if kind == "errors" else kind)
        extra = {"message": 14, "edited_message": 1, "callback_query": 7}.get(kind, 0)
        assert len(actual) == count + extra
        retained = [handler for handler in actual if not is_added_route(handler)]
        expected = [route for route in CONTRACT["routes"] if route["event"] == kind]
        assert len(retained) == len(expected)
        for handler, prior in zip(retained, expected):
            assert handler.callback.__qualname__ == prior["handler"]
            assert aliases(handler) == [alias.lower() for alias in prior["aliases"]]
            assert isinstance(handler.flags["handler_key"], str)
            assert isinstance(handler.flags["fsm_release"], bool)


@pytest.mark.parametrize("case", CONTRACT["cases"], ids=[case["id"] for case in CONTRACT["cases"]])
async def test_captured_selection_through_real_dispatch(case):
    bot = make_bot()
    # Mention matching uses an actual Bot.me() cache without a transport call.
    from aiogram.types import User

    bot._me = User(id=bot.id, is_bot=True, first_name="Synthetic", username="contract_bot")
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(fsm)
    dispatcher.message.middleware(SelectiveIsolationMiddleware())
    dispatcher.message.middleware(Selection())
    dispatcher.include_router(router())
    user = 7 if case.get("actor") == "owner_id" else 8 if case.get("actor") == "founder_ids[0]" else 42
    message = make_message(
        bot,
        from_user=dict(id=user, is_bot=False, first_name="Synthetic"),
        text=case.get("text"),
        caption=case.get("caption"),
        **({"photo": [dict(file_id="photo", file_unique_id="unique", width=1, height=1)]} if "caption" in case else {}),
    )
    if state := case.get("state"):
        context = fsm.resolve_context(bot, message.chat.id, user)
        await context.set_state(state)
    try:
        result = await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, message=message)))
        expected = case.get("approved_target") or case["matched"]
        if expected is None:
            assert result is UNHANDLED
        else:
            handler, meta = result
            assert handler.callback.__qualname__ == expected["handler"]
            if "meta" in case and "approved_target" not in case:
                for key, value in case["meta"].items():
                    assert getattr(meta, key) == value
    finally:
        await fsm.close()
        await bot.session.close()


@pytest.fixture
async def chess_selection_dispatcher():
    bot = make_bot()
    bot._me = User(id=bot.id, is_bot=True, first_name="Synthetic", username="contract_bot")
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(fsm)
    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.middleware(SelectiveIsolationMiddleware())
        observer.middleware(Selection())
    dispatcher.include_router(router())
    try:
        yield bot, dispatcher
    finally:
        await fsm.close()
        await bot.session.close()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/chess", "Chess.process"),
        ("/chess@contract_bot", "Chess.process"),
        ("/CHESS", "Chess.process"),
        ("/CHESS@CONTRACT_BOT", "Chess.process"),
        ("/chess_top", "Chess.top"),
        ("/chess_top@contract_bot", "Chess.top"),
        ("/CHESS_TOP", "Chess.top"),
        ("/CHESS_TOP@CONTRACT_BOT", "Chess.top"),
        ("/chess@another_bot", None),
        ("/chess_top@another_bot", None),
        ("/chess_play", "ChessPlay.process"),
        ("/CHESS_PLAY@CONTRACT_BOT", "ChessPlay.process"),
        ("/chess_rating", "ChessRating.process"),
        ("/CHESS_RATING@CONTRACT_BOT", "ChessRating.process"),
        ("/chess_play@another_bot", None),
        ("/chess_rating@another_bot", None),
        ("/art", "Art.process"),
        ("/ART@CONTRACT_BOT", "Art.process"),
        ("/art_top", "Art.top"),
        ("/ART_TOP@CONTRACT_BOT", "Art.top"),
        ("/art@another_bot", None),
        ("/art_top@another_bot", None),
    ],
)
async def test_chess_commands_select_real_routes_with_case_and_mentions(chess_selection_dispatcher, text, expected):
    bot, dispatcher = chess_selection_dispatcher
    update = Update(update_id=1, message=make_message(bot, text=text))
    result = await asyncio.create_task(dispatcher.feed_update(bot, update))
    if expected is None:
        assert result is UNHANDLED
    else:
        handler, meta = result
        assert handler.callback.__qualname__ == expected
        assert handler.flags["handler_key"] == expected
        assert meta is not None
    assert bot.session.methods == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/reactions", "Reactions.process"),
        ("/реакции", "Reactions.process"),
        ("/REACTIONS@CONTRACT_BOT", "Reactions.process"),
        ("/РЕАКЦИИ@contract_bot", "Reactions.process"),
        ("Посмотрим #reactions", "Reactions.process"),
        ("#реакции", "Reactions.process"),
        ("/reactions@another_bot", None),
        ("/реакции@another_bot", None),
    ],
)
async def test_reaction_aliases_select_the_real_chat_scoreboard_route(chess_selection_dispatcher, text, expected):
    bot, dispatcher = chess_selection_dispatcher
    result = await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, message=make_message(bot, text=text))))
    if expected is None:
        assert result is UNHANDLED
    else:
        handler, meta = result
        assert handler.callback.__qualname__ == expected and handler.flags["handler_key"] == expected
        assert handler.flags["fsm_release"] is True and meta is not None
    assert bot.session.methods == []


@pytest.mark.parametrize("view", ["getters", "givers", "posts", "pulse"])
@pytest.mark.parametrize("days", [1, 7, 30])
async def test_reaction_buttons_select_real_typed_callback_routes(chess_selection_dispatcher, view, days):
    bot, dispatcher = chess_selection_dispatcher
    callback = CallbackQuery(
        id="synthetic",
        chat_instance="synthetic",
        from_user=User(id=42, is_bot=False, first_name="Synthetic"),
        message=make_message(bot),
        data=ReactionCallback(view=view, days=days).pack(),
    )
    handler, _ = await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, callback_query=callback)))
    assert handler.callback.__qualname__ == "Reactions.process_cb"
    assert handler.flags["handler_key"] == "Reactions.process_cb" and handler.flags["fsm_release"] is True
    assert bot.session.methods == []


@pytest.mark.parametrize("payload", ["react:secret:30", "react:getters:31", "react:getters:30:-10099", "react:getters:030"])
async def test_malformed_reaction_buttons_select_expired_fallback(chess_selection_dispatcher, payload):
    bot, dispatcher = chess_selection_dispatcher
    callback = CallbackQuery(
        id="synthetic",
        chat_instance="synthetic",
        from_user=User(id=42, is_bot=False, first_name="Synthetic"),
        message=make_message(bot),
        data=payload,
    )
    handler, _ = await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, callback_query=callback)))
    assert handler.callback.__qualname__ == "process_expired_callback"


@pytest.mark.parametrize("callback", [False, True])
async def test_reaction_navigation_preserves_active_cancel_based_conversations(callback):
    bot = make_bot()
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(fsm)
    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.middleware(SelectiveIsolationMiddleware())
        observer.middleware(Selection())
    dispatcher.include_router(router())
    message = make_message(bot, text="/reactions", message_thread_id=17, is_topic_message=True)
    state = fsm.resolve_context(bot, message.chat.id, 42, thread_id=17)
    await state.set_state("ProgStates:stdin")
    if callback:
        event = Update(
            update_id=1,
            callback_query=CallbackQuery(
                id="synthetic",
                chat_instance="synthetic",
                from_user=User(id=42, is_bot=False, first_name="Synthetic"),
                message=message,
                data=ReactionCallback(view="getters", days=30).pack(),
            ),
        )
    else:
        event = Update(update_id=1, message=message)
    try:
        handler, _ = await asyncio.create_task(dispatcher.feed_update(bot, event))
        assert handler.callback.__qualname__ == ("process_expired_callback" if callback else "ProgCompiler.process_stdin_run")
        assert await state.get_state() == "ProgStates:stdin"
        assert bot.session.methods == []
    finally:
        await fsm.close()
        await dispatcher.fsm.close()
        await bot.session.close()


@pytest.mark.parametrize(
    ("body", "expected", "expected_text"),
    [
        ("/meme кот", "process_meme", "кот"),
        ("/MEME@CONTRACT_BOT кот", "process_meme", "кот"),
        ("кот #meme", "process_meme", "кот"),
        ("/meme@another_bot кот", None, None),
        ("/lobster кот", "process_lobster", "кот"),
        ("/л кот", "process_lobster", "кот"),
        ("кот #l", "process_lobster", "кот"),
        ("/demotivator кот", "process_demotivator", "кот"),
        ("/де кот", "process_demotivator", "кот"),
        ("кот #de", "process_demotivator", "кот"),
    ],
)
@pytest.mark.parametrize("caption", [False, True])
async def test_caption_styles_select_real_routes_in_text_and_media_captions(
    chess_selection_dispatcher, body, expected, expected_text, caption
):
    bot, dispatcher = chess_selection_dispatcher
    fields = (
        {"caption": body, "video": {"file_id": "video", "file_unique_id": "v", "width": 320, "height": 240, "duration": 1}}
        if caption
        else {"text": body}
    )
    result = await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, message=make_message(bot, **fields))))
    if expected is None:
        assert result is UNHANDLED
    else:
        handler, meta = result
        assert handler.flags["handler_key"] == expected
        assert handler.callback.__qualname__ == expected
        assert meta.text == expected_text
    assert bot.session.methods == []


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (ChessCallback(round="round-token", choice="0").pack(), "Chess.process_cb"),
        (ArtCallback(round="round-token", choice="0").pack(), "Art.process_cb"),
        (ArtCallback(round="round-token", choice="finish").pack(), "Art.process_cb"),
        (ChessCallback(round="round-token", choice="finish").pack(), "Chess.process_cb"),
        (GeoguessCallback(round="round-token", choice="0").pack(), "Geoguess.process_cb"),
        (GeoguessCallback(round="round-token", choice="finish").pack(), "Geoguess.process_cb"),
        ("chessboard:round-token:0", "process_expired_callback"),
        ("chess:round-token", "process_expired_callback"),
    ],
)
async def test_chess_and_geoguess_callbacks_select_separate_real_routes(chess_selection_dispatcher, data, expected):
    bot, dispatcher = chess_selection_dispatcher
    callback = CallbackQuery(
        id="synthetic",
        chat_instance="synthetic",
        from_user=User(id=42, is_bot=False, first_name="Synthetic"),
        message=make_message(bot),
        data=data,
    )
    result = await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, callback_query=callback)))
    handler, _ = result
    assert handler.callback.__qualname__ == expected
    assert handler.flags["handler_key"] == expected
    assert bot.session.methods == []


@pytest.mark.parametrize(
    "callback_type,expected",
    [(ChessCallback, "Chess.process_cb"), (GeoguessCallback, "Geoguess.process_cb"), (ArtCallback, "Art.process_cb")],
)
@pytest.mark.parametrize("choice", ["0", "finish", "page_1"])
@pytest.mark.parametrize("conversation", ["ProgStates:stdin", "StickerStates:name", "MakePostStates:message"])
async def test_quiz_buttons_preserve_active_conversations_and_other_topics(callback_type, expected, choice, conversation):
    bot = make_bot()
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(fsm)
    dispatcher.callback_query.middleware(SelectiveIsolationMiddleware())
    dispatcher.callback_query.middleware(Selection())
    dispatcher.include_router(router())
    message = make_message(bot, message_thread_id=17, is_topic_message=True)
    state = fsm.resolve_context(bot, message.chat.id, 42, thread_id=17)
    other = fsm.resolve_context(bot, message.chat.id, 42, thread_id=18)
    await state.set_state(conversation)
    await state.set_data({"draft": "Keep this input"})
    await other.set_state("ProgStates:stdin")
    await other.set_data({"draft": "Another topic"})
    query = CallbackQuery(
        id="synthetic",
        chat_instance="synthetic",
        from_user=User(id=42, is_bot=False, first_name="Synthetic"),
        message=message,
        data=callback_type(round="round-token", choice=choice).pack(),
    )
    try:
        handler, _ = await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, callback_query=query)))
        assert handler.callback.__qualname__ == expected and handler.flags["fsm_release"] is True
        assert await state.get_state() == conversation
        assert await state.get_data() == {"draft": "Keep this input"}
        assert await other.get_state() == "ProgStates:stdin"
        assert await other.get_data() == {"draft": "Another topic"}
        assert bot.session.methods == []
    finally:
        await fsm.close()
        await dispatcher.fsm.close()
        await bot.session.close()


@pytest.mark.parametrize("callback_type", [ChessCallback, GeoguessCallback, ArtCallback])
@pytest.mark.parametrize("missing", ["inaccessible", "inline"])
async def test_quiz_buttons_without_an_accessible_chat_use_the_expired_fallback(chess_selection_dispatcher, callback_type, missing):
    bot, dispatcher = chess_selection_dispatcher
    message = make_message(bot)
    query = CallbackQuery(
        id="synthetic",
        chat_instance="synthetic",
        from_user=User(id=42, is_bot=False, first_name="Synthetic"),
        message=InaccessibleMessage(chat=message.chat, message_id=message.message_id, date=0) if missing == "inaccessible" else None,
        inline_message_id="synthetic" if missing == "inline" else None,
        data=callback_type(round="round-token", choice="0").pack(),
    )
    handler, _ = await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, callback_query=query)))
    assert handler.callback.__qualname__ == "process_expired_callback"


@pytest.mark.parametrize("command", ["/chess", "/chess_top", "/geoguess", "/geoguess_top", "/art", "/art_top"])
async def test_quiz_commands_still_belong_to_active_stdin_until_cancel(command):
    bot = make_bot()
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(fsm)
    dispatcher.message.middleware(Selection())
    dispatcher.include_router(router())
    message = make_message(bot, text=command, message_thread_id=17, is_topic_message=True)
    state = fsm.resolve_context(bot, message.chat.id, 42, thread_id=17)
    await state.set_state("ProgStates:stdin")
    try:
        handler, _ = await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, message=message)))
        assert handler.callback.__qualname__ == "ProgCompiler.process_stdin_run"
        cancel = message.model_copy(update={"text": "/cancel"})
        handler, _ = await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=2, message=cancel)))
        assert handler.callback.__qualname__ == "process_cancel"
    finally:
        await fsm.close()
        await dispatcher.fsm.close()
        await bot.session.close()


@pytest.mark.parametrize("command", ["start", "cancel", "beer", "arxiv", "stt"])
async def test_plain_slash_commands_keep_case_and_caption_policy(command):
    bot = make_bot()
    f = SlashCommand(command)
    try:
        assert await f(make_message(bot, text="/" + command.upper()), bot)
        assert not await f(make_message(bot, caption="/" + command.upper()), bot)
    finally:
        await bot.session.close()


async def test_inline_tyan_callback_is_acknowledged_without_chat_preferences():
    bot = make_bot()
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(StateContextMiddleware())
    fsm = TopicFSMContextMiddleware(MemoryStorage(), ReleasableEventIsolation())
    dispatcher.update.outer_middleware(fsm)
    # No chat exists: preferences must neither access the database nor be required
    # to enter the callback's stale-message guard.
    dispatcher.callback_query.outer_middleware(SettingsMiddleware(None))
    failures = []

    async def capture_error(event):
        failures.append(event.exception)
        return True

    dispatcher.errors.register(capture_error)
    dispatcher.include_router(router())
    update = Update.model_validate(
        {
            "update_id": 1,
            "callback_query": {
                "id": "synthetic",
                "inline_message_id": "synthetic",
                "chat_instance": "synthetic",
                "data": "tyan:sfw:neko",
                "from_user": {"id": 42, "is_bot": False, "first_name": "Synthetic"},
            },
        }
    )
    try:
        await dispatcher.feed_update(bot, update)
        assert failures == []
        assert [method.__api_method__ for method in bot.session.methods] == ["answerCallbackQuery"]
        assert "недоступна" in bot.session.methods[0].text
    finally:
        await fsm.close()
        await bot.session.close()


async def test_ignored_chat_keeps_metrics_without_spending_command_trace_budget(monkeypatch):
    from msu_hub_bot import app

    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    private_text = "SYNTHETIC_PRIVATE_ROUTER_CANARY"
    session, db = RecordingSession(), AsyncMock()
    db.feature_request.return_value = None
    db.load_settings.return_value = {}
    db.list_directory.return_value = []
    monkeypatch.setattr(app, "AiohttpSession", lambda **kwargs: session)
    monkeypatch.setattr(app, "create_repository", lambda *args, **kwargs: db)
    sink = Capture()
    telemetry = Telemetry(config(traces_per_minute=1), transport=sink)
    monkeypatch.setattr(app, "Telemetry", lambda config: telemetry)
    application = await app.Application.create(
        Settings(
            bot_token="123456789:" + "a" * 35,
            supabase_url="http://supabase.invalid",
            supabase_key="synthetic-publishable-key",
            supabase_email="bot@example.invalid",
            supabase_password="synthetic-password",
        )
    )
    try:
        await telemetry.start()
        # Exercise a preference cache miss, a cache hit and both archive jobs
        # through the actual composition root, without mocking handler selection.
        for update_id in (1, 2):
            message = make_message(application.bot, message_id=update_id, text=private_text)
            result = await asyncio.create_task(
                application.dispatcher.feed_update(application.bot, Update(update_id=update_id, message=message))
            )
            assert result is UNHANDLED
        assert session.methods == []
        message = make_message(application.bot, message_id=3, text="/roll")
        await asyncio.create_task(application.dispatcher.feed_update(application.bot, Update(update_id=3, message=message)))
    finally:
        await application.close()

    spans = sink.spans()
    assert sorted(span.name for span in spans) == ["bot.handler", "telegram.request"]
    handler = next(span for span in spans if span.name == "bot.handler")
    request = next(span for span in spans if span.name == "telegram.request")
    assert request.parent_span_id == handler.span_id and request.trace_id == handler.trace_id
    assert any(attr.key == "operation" and attr.value.string_value == "process_roll" for attr in handler.attributes)
    assert any(attr.key == "command" and attr.value.string_value == "roll" for attr in handler.attributes)
    assert any(attr.key == "telegram.method" and attr.value.string_value == "sendMessage" for attr in request.attributes)
    assert not any(span.events for span in spans)
    assert len(sink.logs()) == 1 and sink.logs()[0].span_id == handler.span_id
    assert [method.__api_method__ for method in session.methods] == ["sendMessage"]
    assert [call.args[0].handled for call in db.archive_update.await_args_list] == [False, False, True]
    db.load_settings.assert_awaited_once()
    payload = sink.serialized()
    assert "settings.load" in payload and "archive.write" in payload and "ignored" in payload
    assert "supabase" in payload
    assert private_text not in payload


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/remind in 15m чай", "Remind.process"),
        ("/feedback Last few commands failed", "Feedback.process"),
        ("/FEEDBACK Add chess leaderboard", "Feedback.process"),
        ("/НАПОМНИ через 1ч чай", "Remind.process"),
        ("/app", "process_app"),
        ("/start app_synthetic", "process_app_start"),
        ("/start", "process_start"),
    ],
)
async def test_reminder_and_mini_app_entry_points(chess_selection_dispatcher, text, expected):
    bot, dispatcher = chess_selection_dispatcher
    message = make_message(bot, text=text)
    message = message.model_copy(update={"chat": message.chat.model_copy(update={"type": "private"})})
    selected, _ = await dispatcher.feed_update(bot, Update(update_id=90, message=message))
    assert selected.flags["handler_key"] == expected


@pytest.mark.parametrize("payload", ["raffle:reg", "raffle:winner", "raffle:reg:broken:0"])
async def test_unresolvable_raffle_buttons_use_the_expired_fallback(chess_selection_dispatcher, payload):
    bot, dispatcher = chess_selection_dispatcher
    callback = CallbackQuery(
        id="synthetic",
        chat_instance="synthetic",
        from_user=User(id=42, is_bot=False, first_name="Synthetic"),
        message=make_message(bot),
        data=payload,
    )
    handler, _ = await asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, callback_query=callback)))
    assert handler.callback.__qualname__ == "process_expired_callback"
    assert bot.session.methods == []
