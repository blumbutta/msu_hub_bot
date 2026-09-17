"""GeoGuess delivery and task ownership through real aiogram objects."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageCaption, EditMessageText, SendMessage, SendPhoto
from aiogram.types import CallbackQuery
from datetime import date

from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.geoguess import Photo
from msu_hub_bot.telegram.runtime import Supervisor
from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.commands import geoguess as game
from telegram_helpers import RecordingSession, make_message


PHOTO = Photo(
    "Норвегия",
    "Берген",
    "https://upload.wikimedia.org/test.jpg",
    "https://commons.wikimedia.org/?curid=1",
    "Author <name>",
    "CC BY 3.0",
    "https://creativecommons.org/licenses/by/3.0",
)


class GameSession(RecordingSession):
    def __init__(self):
        super().__init__()
        self.timeouts = []
        self.messages = {}
        self.caption_error = False

    async def make_request(self, bot, method, timeout=None):
        self.timeouts.append(timeout)
        if isinstance(method, (SendMessage, SendPhoto)):
            self.methods.append(method)
            message = make_message(
                bot,
                message_id=100 + len(self.messages),
                chat={"id": method.chat_id, "type": "supergroup"},
                message_thread_id=method.message_thread_id,
                is_topic_message=method.message_thread_id is not None,
            )
            self.messages[message.message_id] = message
            return message
        if isinstance(method, (EditMessageText, EditMessageCaption)):
            self.methods.append(method)
            if isinstance(method, EditMessageCaption) and self.caption_error:
                raise TelegramBadRequest(method=method, message="message can't be edited")
            return self.messages[method.message_id]
        return await super().make_request(bot, method, timeout)


@pytest.fixture
async def rig(monkeypatch):
    monkeypatch.setattr(game.Geoguess, "rounds", {})
    monkeypatch.setattr(game.Geoguess, "recent_countries", game.LRUCache(maxsize=1024))
    monkeypatch.setattr(game.Geoguess, "completed", game.TTLCache(maxsize=128, ttl=game.RESULT_TTL))
    monkeypatch.setattr(game, "EDIT_INTERVAL", 0)
    monkeypatch.setattr(game, "random_photo", AsyncMock(return_value=PHOTO))
    session = GameSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    message = make_message(bot, message_id=10, is_topic_message=True, message_thread_id=17)
    client = SimpleNamespace(eval=AsyncMock(return_value=1), zrevrange=AsyncMock(return_value=[]), hget=AsyncMock())
    supervisor = Supervisor()
    yield SimpleNamespace(
        bot=bot,
        session=session,
        message=message,
        client=client,
        redis=SimpleNamespace(redis=AsyncMock(return_value=client)),
        supervisor=supervisor,
    )
    await game.Geoguess.shutdown()
    await supervisor.drain(timeout=1, cancel_timeout=0.5)
    await session.close()


async def click(rig, round_, choice, user_id=42, token=None):
    data = game.GeoguessCallback(round=token or round_.token, choice=str(choice)).pack()
    query = CallbackQuery.model_validate(
        {
            "id": "synthetic",
            "chat_instance": "synthetic",
            "message": round_.message,
            "data": data,
            "from_user": {"id": user_id, "is_bot": False, "first_name": "User <name>", "username": "user_name"},
        },
        context={"bot": rig.bot},
    )
    return await game.Geoguess.process_cb(query, game.GeoguessCallback.unpack(data), rig.redis, rig.supervisor)


async def start(rig):
    await game.Geoguess.process(rig.message, rig.redis, rig.supervisor)
    return game.Geoguess.rounds[rig.message.chat.id]


def reveals(rig):
    return [
        method for method in rig.session.methods if isinstance(method, EditMessageCaption) and method.caption.startswith("🌍 На снимке")
    ]


async def test_round_uses_real_shortcuts_and_scores_once(rig):
    round_ = await start(rig)
    await game.Geoguess.process(rig.message, rig.redis, rig.supervisor)
    correct = round_.options.index(PHOTO.country)
    await click(rig, round_, correct)
    await click(rig, round_, (correct + 1) % 6)
    await click(rig, round_, correct, user_id=43, token="stale")
    assert round_.votes == {42: (correct, "User <name>")}
    await asyncio.gather(click(rig, round_, "finish"), click(rig, round_, "finish"))
    await click(rig, round_, correct, user_id=44)
    await rig.supervisor.drain(timeout=1, cancel_timeout=0.5)

    rig.client.eval.assert_awaited_once()
    assert game.score_key(rig.message.chat.id) in rig.client.eval.call_args.args
    assert not game.Geoguess.rounds and rig.supervisor.job_count == 0
    methods = rig.session.methods
    photos = [method for method in methods if isinstance(method, SendPhoto)]
    assert len(photos) == 1 and photos[0].photo == PHOTO.url
    assert photos[0].reply_parameters.message_id == rig.message.message_id
    assert len([button for row in photos[0].reply_markup.inline_keyboard for button in row]) == 7
    assert {type(method) for method in methods} == {SendPhoto, SendMessage, AnswerCallbackQuery, EditMessageCaption}
    assert all(method.message_thread_id == 17 for method in methods if isinstance(method, (SendMessage, SendPhoto)))
    replies = [method for method in methods if isinstance(method, SendMessage)]
    assert len(replies) == 1 and "прошлое задание" in replies[0].text
    edits = [method for method in methods if isinstance(method, EditMessageCaption)]
    assert len(edits) == 2 and all(method.message_id == round_.message.message_id for method in edits)
    assert "Ответили: 1" in edits[0].caption and all(country not in edits[0].caption for country in round_.options)
    results = reveals(rig)
    assert len(results) == 1
    result = results[0]
    assert "Берген" in result.caption and "Норвегия" in result.caption and "User <name>" in result.caption
    mention = next(entity for entity in result.caption_entities if entity.url == "tg://user?id=42")
    assert mention.extract_from(result.caption) == "User <name>"
    assert result.parse_mode is None and result.reply_markup is None
    assert all(timeout == game.SEND_TIMEOUT for timeout in rig.session.timeouts)
    assert all("request_timeout" not in method.model_extra for method in methods)


async def test_failed_caption_edit_preserves_one_message_and_can_retry(rig):
    round_ = await start(rig)
    initial_view = round_.view
    rig.session.caption_error = True
    await click(rig, round_, "finish")
    result = rig.session.methods[-1]
    assert isinstance(result, EditMessageCaption) and "Берген" in result.caption and "Норвегия" in result.caption
    assert result.message_id == round_.message.message_id and result.chat_id == round_.message.chat.id
    assert result.parse_mode is None and result.caption_entities
    assert round_.view == initial_view
    assert not any(isinstance(method, SendMessage) for method in rig.session.methods)
    assert not game.Geoguess.rounds
    assert game.Geoguess.completed[round_.message.chat.id, round_.token] is round_
    rig.session.caption_error = False
    await click(rig, round_, "page_0")
    assert isinstance(rig.session.methods[-1], EditMessageCaption)
    assert rig.session.methods[-1].message_id == round_.message.message_id
    assert round_.view.caption == result.caption and round_.view != initial_view
    assert not any(isinstance(method, SendMessage) for method in rig.session.methods)


async def test_leaderboard_uses_real_shortcuts_when_storage_fails(rig):
    rig.client.zrevrange.return_value = [(b"42", 3)]
    rig.client.hget.side_effect = [b"User <name>", b"user_name"]
    await game.Geoguess.top(rig.message, rig.redis)
    result = rig.session.methods[-1]
    assert "User <name>" in result.text
    assert "@user_name" in result.text and "— 3" in result.text
    assert result.parse_mode is None
    mention = next(entity for entity in result.entities if entity.url == "tg://user?id=42")
    assert mention.extract_from(result.text) == "User <name>"
    rig.client.zrevrange.side_effect = RuntimeError("synthetic storage failure")
    await game.Geoguess.top(rig.message, rig.redis)
    result = rig.session.methods[-1]
    assert isinstance(result, SendMessage) and result.text == "Рейтинг сейчас недоступен."
    assert result.reply_parameters.message_id == rig.message.message_id and result.message_thread_id == 17


async def test_failed_photo_releases_round_and_replies(rig):
    game.random_photo.side_effect = ExternalServiceError("synthetic photo failure")
    await game.Geoguess.process(rig.message, rig.redis, rig.supervisor)
    result = rig.session.methods[-1]
    assert isinstance(result, SendMessage) and result.text == "Ошибка, попробуйте еще раз"
    assert result.message_thread_id == 17 and not game.Geoguess.rounds


async def test_cancelled_callback_does_not_cancel_started_finish(rig):
    round_ = await start(rig)
    await click(rig, round_, round_.options.index(PHOTO.country))
    entered, release = asyncio.Event(), asyncio.Event()

    async def save(*args):
        entered.set()
        await release.wait()

    rig.client.eval.side_effect = save
    worker = asyncio.create_task(click(rig, round_, "finish"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert rig.supervisor.job_count == 1 and not round_.task.done()
        release.set()
        drained = await rig.supervisor.drain(timeout=1, cancel_timeout=0.5)
        assert drained.cancelled_jobs == drained.failed_jobs == 0
        await click(rig, round_, "finish")
        rig.client.eval.assert_awaited_once()
        assert len(reveals(rig)) == 1
        assert not game.Geoguess.rounds and rig.supervisor.job_count == 0
    finally:
        release.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_send_deadline_cancels_blocked_request_middleware(rig, monkeypatch):
    monkeypatch.setattr(game, "SEND_TIMEOUT", 0.01)
    cancelled = asyncio.Event()

    async def blocked(make_request, bot, method):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    rig.session.middleware(blocked)
    request = asyncio.create_task(game._send(rig.message.reply("synthetic")))
    try:
        done, _ = await asyncio.wait({request}, timeout=0.5)
        assert request in done, "The GeoGuess deadline must include request middleware"
        with pytest.raises(TimeoutError):
            await request
        assert cancelled.is_set() and not rig.session.methods
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def test_timer_expires_after_photo_and_reveals_once(rig, monkeypatch):
    monkeypatch.setattr(game, "ROUND_TIMEOUT", 0.02)
    round_ = await start(rig)
    assert rig.supervisor.job_count == 0
    await click(rig, round_, round_.options.index(PHOTO.country))
    await asyncio.sleep(0.05)
    await rig.supervisor.drain(timeout=1, cancel_timeout=0.5)
    assert round_.closed and round_.timer.cancelled()
    rig.client.eval.assert_awaited_once()
    assert not game.Geoguess.rounds
    assert len(reveals(rig)) == 1


async def test_manual_finish_and_due_timer_share_one_completion(rig, monkeypatch):
    monkeypatch.setattr(game, "ROUND_TIMEOUT", 0.02)
    round_ = await start(rig)
    await click(rig, round_, round_.options.index(PHOTO.country))
    entered, release = asyncio.Event(), asyncio.Event()

    async def save(*args):
        entered.set()
        await release.wait()

    rig.client.eval.side_effect = save
    manual = asyncio.create_task(click(rig, round_, "finish"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0.05)
        assert round_.closed and round_.timer.cancelled()
        assert rig.client.eval.await_count == 1
        release.set()
        await manual
        await rig.supervisor.drain(timeout=1, cancel_timeout=0.5)
        rig.client.eval.assert_awaited_once()
        assert len(reveals(rig)) == 1
        assert not game.Geoguess.rounds
    finally:
        release.set()
        manual.cancel()
        await asyncio.gather(manual, return_exceptions=True)


async def test_shutdown_cancels_timer_without_revealing_or_scoring(rig, monkeypatch):
    monkeypatch.setattr(game, "ROUND_TIMEOUT", 0.02)
    round_ = await start(rig)
    await game.Geoguess.shutdown()
    await asyncio.sleep(0.05)
    assert round_.timer.cancelled() and not game.Geoguess.rounds
    assert rig.supervisor.job_count == 0
    rig.client.eval.assert_not_awaited()
    assert not any(isinstance(method, EditMessageCaption) for method in rig.session.methods)


async def test_loading_does_not_consume_round_deadline(rig, monkeypatch):
    monkeypatch.setattr(game, "ROUND_TIMEOUT", 0.05)
    entered, release = asyncio.Event(), asyncio.Event()

    async def photo(recent_countries):
        entered.set()
        await release.wait()
        return PHOTO

    game.random_photo.side_effect = photo
    worker = asyncio.create_task(start(rig))
    try:
        await entered.wait()
        round_ = game.Geoguess.rounds[rig.message.chat.id]
        assert round_.timer is None
        await asyncio.sleep(0.07)
        assert not round_.closed and round_.timer is None
        release.set()
        await worker
        assert round_.timer is not None and not round_.timer.cancelled()
        assert not round_.closed
    finally:
        release.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_leaderboard_captures_one_day_across_midnight(rig, monkeypatch):
    first, second = date(2026, 9, 17), date(2026, 9, 18)
    current = first
    monkeypatch.setattr(game, "today", lambda: current)

    async def scores(*args, **kwargs):
        nonlocal current
        current = second
        return [(b"42", 3)]

    rig.client.zrevrange.side_effect = scores
    rig.client.hget.side_effect = [b"User <name>", b"user_name"]
    await game.Geoguess.top(rig.message, rig.redis)
    rig.client.zrevrange.assert_awaited_once_with(game.score_key(rig.message.chat.id, first), 0, 9, withscores=True)
    assert all(call.args[0].startswith(game.score_key(rig.message.chat.id, first)) for call in rig.client.hget.call_args_list)
    assert "17.09.2026" in rig.session.methods[-1].text


async def test_new_day_does_not_read_yesterdays_ranking(rig, monkeypatch):
    previous, current = date(2026, 9, 17), date(2026, 9, 18)
    day = previous
    monkeypatch.setattr(game, "today", lambda: day)
    rig.client.zrevrange.side_effect = [[(b"42", 3)], []]
    rig.client.hget.side_effect = [b"User <name>", b"user_name"]
    await game.Geoguess.top(rig.message, rig.redis)
    assert "@user_name" in rig.session.methods[-1].text
    day = current
    await game.Geoguess.top(rig.message, rig.redis)
    assert "@user_name" not in rig.session.methods[-1].text
    assert "Пока нет очков" in rig.session.methods[-1].text
    assert [call.args[0] for call in rig.client.zrevrange.call_args_list] == [
        game.score_key(rig.message.chat.id, previous),
        game.score_key(rig.message.chat.id, current),
    ]


async def test_timer_first_rejects_finish_click_while_scoring(rig, monkeypatch):
    monkeypatch.setattr(game, "ROUND_TIMEOUT", 0.02)
    round_ = await start(rig)
    await click(rig, round_, round_.options.index(PHOTO.country))
    entered, release = asyncio.Event(), asyncio.Event()

    async def save(*args):
        entered.set()
        await release.wait()

    rig.client.eval.side_effect = save
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert round_.closed
        await click(rig, round_, "finish")
        answer = rig.session.methods[-1]
        assert isinstance(answer, AnswerCallbackQuery) and "Раунд завершён" in answer.text
        assert rig.client.eval.await_count == 1
        release.set()
        await rig.supervisor.drain(timeout=1, cancel_timeout=0.5)
        assert len(reveals(rig)) == 1
        assert not game.Geoguess.rounds
    finally:
        release.set()


async def test_stale_timer_cannot_finish_replacement_round(rig):
    previous = await start(rig)
    await click(rig, previous, "finish")
    current = await start(rig)
    assert game.Geoguess.start_finish(rig.message.chat.id, previous, rig.redis, rig.supervisor) is None
    assert game.Geoguess.rounds[rig.message.chat.id] is current and not current.closed
    assert current.timer is not None and not current.timer.cancelled()


async def test_timer_during_closed_admission_releases_round_cleanly(rig):
    round_ = await start(rig)
    rig.supervisor.close_updates()
    assert game.Geoguess.start_finish(rig.message.chat.id, round_, rig.redis, rig.supervisor) is None
    assert round_.closed and round_.timer.cancelled() and not game.Geoguess.rounds
    assert rig.supervisor.job_count == 0
    rig.client.eval.assert_not_awaited()
