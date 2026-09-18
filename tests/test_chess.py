"""Chess rounds through real aiogram messages and a network-free transport."""

import asyncio
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageCaption, EditMessageMedia, EditMessageText, SendMessage, SendPhoto
from aiogram.types import BufferedInputFile, CallbackQuery

from msu_hub_bot.commands import chess as game
from msu_hub_bot.commands import geoguess
from msu_hub_bot.providers.chess import MoveOption, Puzzle
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.telegram.runtime import Supervisor
from msu_hub_bot.telegram.wrapper import BotWrapper
from telegram_helpers import RecordingSession, make_message


DAY = date(2026, 9, 17)
REAL_TODAY = game.today
PNG = b"\x89PNG\r\n\x1a\nsynthetic-board"
PUZZLE = Puzzle(
    id="X0FOH",
    fen="rkb2R2/p1p4p/1pB1p3/2n1q3/8/P1p5/1PP3PP/1K3R2 w - - 0 1",
    solution=("f8c8", "b8c8", "f1f8"),
    options=(
        MoveOption("f8f7", "Ладья f8 → f7"),
        MoveOption("f1f7", "Ладья f1 → f7"),
        MoveOption("c6d5", "Слон c6 → d5"),
        MoveOption("f8c8", "Ладья f8 → c8"),
        MoveOption("c6b5", "Слон c6 → b5"),
        MoveOption("f1e1", "Ладья f1 → e1"),
    ),
    line=("Rxc8+", "Kxc8", "Rf8#"),
)


class GameSession(RecordingSession):
    def __init__(self):
        super().__init__()
        self.messages = {}
        self.timeouts = []
        self.photo_hook = None
        self.media_error = False
        self.caption_error = False
        self.edit_hook = None

    async def make_request(self, bot, method, timeout=None):
        self.timeouts.append(timeout)
        if isinstance(method, SendPhoto) and self.photo_hook is not None:
            await self.photo_hook(method)
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
        if isinstance(method, (EditMessageText, EditMessageCaption, EditMessageMedia)):
            self.methods.append(method)
            if self.edit_hook is not None:
                await self.edit_hook(method)
            if isinstance(method, EditMessageMedia) and self.media_error:
                raise TelegramBadRequest(method=method, message="message can't be edited")
            if isinstance(method, EditMessageCaption) and self.caption_error:
                raise TelegramBadRequest(method=method, message="caption can't be edited")
            return self.messages[method.message_id]
        return await super().make_request(bot, method, timeout)


class ScoreStore:
    """Redis test double implementing the round-scoring adapter's atomic contract."""

    def __init__(self):
        self.scores = defaultdict(dict)
        self.hashes = defaultdict(dict)
        self.rounds = defaultdict(set)
        self.expiries = {}
        self.eval = AsyncMock(side_effect=self.apply)
        self.zrevrange = AsyncMock(side_effect=self.ranking)
        self.hget = AsyncMock(side_effect=lambda key, uid: self.hashes[key].get(str(uid)))

    async def apply(self, script, numkeys, *args):
        assert numkeys == 4
        key, names, usernames, rounds, expires, token, *players = args
        if token in self.rounds[rounds]:
            return 0
        for index in range(0, len(players), 4):
            uid, name, username, delta = players[index : index + 4]
            self.scores[key][uid] = max(0, self.scores[key].get(uid, 0) + delta)
            self.hashes[names][uid] = name
            self.hashes[usernames][uid] = username
        self.rounds[rounds].add(token)
        self.expiries.update(dict.fromkeys((key, names, usernames, rounds), expires))
        return 1

    async def ranking(self, key, first, last, *, withscores):
        assert withscores
        return sorted(self.scores[key].items(), key=lambda item: item[1], reverse=True)[first : last + 1]


@pytest.fixture
async def rig(monkeypatch, saved_quizzes):
    monkeypatch.setattr(game.Chess, "rounds", {})
    monkeypatch.setattr(game.Chess, "recent_puzzles", game.LRUCache(maxsize=1024))
    monkeypatch.setattr(game.Chess, "completed", game.TTLCache(maxsize=game.MAX_ROUNDS, ttl=game.RESULT_TTL))
    monkeypatch.setattr(game, "today", lambda: DAY)
    monkeypatch.setattr(game, "EDIT_INTERVAL", 0)
    monkeypatch.setattr(game, "random_puzzle", AsyncMock(return_value=PUZZLE))
    monkeypatch.setattr(game, "render_board", Mock(return_value=PNG))
    session = GameSession()
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    client = ScoreStore()
    supervisor = Supervisor()
    yield SimpleNamespace(
        bot=bot,
        session=session,
        message=make_message(bot, message_id=10, is_topic_message=True, message_thread_id=17),
        client=client,
        redis=SimpleNamespace(redis=AsyncMock(return_value=client)),
        supervisor=supervisor,
        snapshots=saved_quizzes,
    )
    await game.Chess.shutdown()
    await supervisor.drain(timeout=1, cancel_timeout=0.5)
    await session.close()


async def start(rig, message=None):
    message = message or rig.message
    await game.Chess.process(message, rig.redis, rig.supervisor)
    return game.Chess.rounds[message.chat.id]


async def click(rig, round_, choice, *, user_id=42, token=None, message=None, username="user_name"):
    data = game.ChessCallback(round=token or round_.token, choice=str(choice))
    query = CallbackQuery.model_validate(
        {
            "id": "synthetic",
            "chat_instance": "synthetic",
            "message": message or round_.message,
            "data": data.pack(),
            "from_user": {"id": user_id, "is_bot": False, "first_name": "User <name>", "username": username},
        },
        context={"bot": rig.bot},
    )
    return await game.Chess.process_cb(query, data, rig.redis, rig.supervisor)


def correct(round_):
    return next(index for index, option in enumerate(round_.options) if option.uci == round_.puzzle.solution[0])


def text_of(method):
    if isinstance(method, EditMessageMedia):
        return method.media.caption or ""
    return getattr(method, "text", None) or getattr(method, "caption", None) or ""


def reveals(rig):
    return [method for method in rig.session.methods if isinstance(method, EditMessageMedia)]


def captions(rig):
    return [method for method in rig.session.methods if isinstance(method, (EditMessageCaption, EditMessageMedia))]


def buttons(round_):
    return [button for row in round_.markup.inline_keyboard for button in row] if round_.markup is not None else []


async def all_pages(rig, round_):
    rendered = []
    for page in range(round_.view.pages):
        await click(rig, round_, f"page_{page}")
        rendered.append(round_.view)
    return rendered


@pytest.mark.parametrize("side,label", [("w", "бел"), ("b", "чёр")])
async def test_photo_has_six_moves_and_side_without_revealing_solution(rig, side, label):
    game.random_puzzle.return_value = replace(PUZZLE, fen=PUZZLE.fen.replace(" w ", f" {side} "))
    round_ = await start(rig)
    await game.Chess.process(rig.message, rig.redis, rig.supervisor)
    photos = [method for method in rig.session.methods if isinstance(method, SendPhoto)]
    assert len(photos) == 1
    photo = photos[0]
    assert isinstance(photo.photo, BufferedInputFile) and photo.photo.data == PNG
    assert label in photo.caption.lower()
    assert all(value not in photo.caption for value in (PUZZLE.id, "lichess", "Rxc8", "f8c8", "sacrifice", PUZZLE.fen))
    buttons = [button for row in photo.reply_markup.inline_keyboard for button in row]
    assert len(buttons) == 7 and len({option.uci for option in round_.options}) == 6
    assert sum(option.uci == PUZZLE.solution[0] for option in round_.options) == 1
    assert [button.text for button in buttons[:-1]] == [option.label for option in round_.options]
    assert [game.ChessCallback.unpack(button.callback_data).choice for button in buttons[:-1]] == list(map(str, range(6)))
    assert game.ChessCallback.unpack(buttons[-1].callback_data).choice == "finish"
    assert photo.reply_parameters.message_id == rig.message.message_id and photo.message_thread_id == 17
    assert round_.timer is not None and game.ROUND_TIMEOUT == 600 and game.PHOTO_TIMEOUT == 10
    game.random_puzzle.assert_awaited_once()


async def test_votes_hide_choices_and_finish_reveals_everyone_with_signed_points(rig):
    round_ = await start(rig)
    answer = correct(round_)
    await click(rig, round_, answer, user_id=42)
    await click(rig, round_, (answer + 1) % 6, user_id=43, username=None)
    await click(rig, round_, (answer + 1) % 6, user_id=42)
    hidden = round_.view.caption
    assert "2" in hidden and "User <name>" in hidden and "@user_name" in hidden
    assert any(entity.url == "tg://user?id=43" for entity in round_.view.entities)
    assert all(option.label not in hidden for option in round_.options)
    assert round_.votes[42][0] == answer
    await asyncio.gather(click(rig, round_, "finish", user_id=99), click(rig, round_, "finish", user_id=98))
    await click(rig, round_, answer, user_id=44)
    assert 44 not in round_.votes and not game.Chess.rounds
    rig.client.eval.assert_awaited_once()
    assert rig.client.scores[game.score_key(rig.message.chat.id)] == {"42": 1, "43": 0}
    deltas = rig.client.eval.call_args.args[8:]
    assert deltas == ("42", "User <name>", "user_name", 1, "43", "User <name>", "", -1)
    pages = await all_pages(rig, round_)
    final = "\n".join(page.caption for page in pages)
    assert round_.options[answer].label in final and round_.options[(answer + 1) % 6].label in final
    assert "✓" in final and "✗" in final and "@user_name" in final
    assert any(entity.url == "tg://user?id=43" for page in pages for entity in page.entities)
    assert len(reveals(rig)) == 1
    assert all(game.ChessCallback.unpack(button.callback_data).choice.startswith("page_") for button in buttons(round_))
    assert all(move in text_of(reveals(rig)[0]) for move in PUZZLE.line)
    assert any(entity.url == f"https://lichess.org/training/{PUZZLE.id}" for entity in reveals(rig)[0].media.caption_entities)
    game.render_board.assert_called_with(PUZZLE.fen, arrow=PUZZLE.solution[0])
    assert all(method.message_thread_id == 17 for method in rig.session.methods if isinstance(method, (SendMessage, SendPhoto)))
    assert all("request_timeout" not in method.model_extra for method in rig.session.methods)


@pytest.mark.parametrize("choice", ["bogus", "-1", "6", "1.5"])
async def test_invalid_options_do_not_vote(rig, choice):
    round_ = await start(rig)
    await click(rig, round_, choice)
    assert not round_.votes
    assert isinstance(rig.session.methods[-1], AnswerCallbackQuery)
    assert "Неизвестный" in rig.session.methods[-1].text


async def test_stale_token_wrong_message_and_old_timer_cannot_change_new_round(rig):
    old = await start(rig)
    await click(rig, old, 0, token="old-token")
    await click(rig, old, 0, message=rig.message)
    assert not old.votes
    await click(rig, old, "finish")
    current = await start(rig)
    await click(rig, old, 0)
    assert game.Chess.start_finish(rig.message.chat.id, old, rig.redis, rig.supervisor) is None
    assert not current.closed and not current.votes and not current.timer.cancelled()


@pytest.mark.parametrize("timer_first", [False, True])
async def test_timer_and_manual_finish_score_and_reveal_once(rig, monkeypatch, timer_first):
    monkeypatch.setattr(game, "ROUND_TIMEOUT", 0.02)
    round_ = await start(rig)
    await click(rig, round_, correct(round_))
    entered, release = asyncio.Event(), asyncio.Event()

    async def save(*args):
        entered.set()
        await release.wait()
        return 1

    rig.client.eval.side_effect = save
    manual = None
    try:
        if not timer_first:
            manual = asyncio.create_task(click(rig, round_, "finish"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0.04)
        if timer_first:
            await click(rig, round_, "finish")
        assert round_.closed and round_.timer.cancelled()
        rig.client.eval.assert_awaited_once()
        release.set()
        if manual is not None:
            await manual
        await rig.supervisor.drain(timeout=1, cancel_timeout=0.5)
        assert len(reveals(rig)) == 1 and not game.Chess.rounds
    finally:
        release.set()
        if manual is not None:
            manual.cancel()
            await asyncio.gather(manual, return_exceptions=True)


async def test_timer_starts_only_after_photo_delivery(rig, monkeypatch):
    monkeypatch.setattr(game, "ROUND_TIMEOUT", 0.03)
    entered, release = asyncio.Event(), asyncio.Event()

    async def photo(method):
        entered.set()
        await release.wait()

    rig.session.photo_hook = photo
    worker = asyncio.create_task(start(rig))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        round_ = game.Chess.rounds[rig.message.chat.id]
        await asyncio.sleep(0.05)
        assert round_.timer is None and not round_.closed
        assert rig.message.chat.id not in game.Chess.recent_puzzles
        release.set()
        assert await worker is round_
        assert round_.timer is not None and not round_.closed
    finally:
        release.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("stage", ["fetch", "render", "send"])
async def test_start_deadline_cancels_each_stage_without_remembering_failed_puzzle(rig, monkeypatch, stage):
    monkeypatch.setattr(game, "PHOTO_TIMEOUT", 0.02)
    cancelled = asyncio.Event()
    history = ("old01", "old02")
    game.Chess.recent_puzzles[rig.message.chat.id] = history

    async def blocked(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    if stage == "fetch":
        game.random_puzzle.side_effect = blocked
    elif stage == "render":
        monkeypatch.setattr(game.asyncio, "to_thread", blocked)
    else:
        rig.session.photo_hook = blocked
    await game.Chess.process(rig.message, rig.redis, rig.supervisor)
    assert cancelled.is_set() and not game.Chess.rounds
    assert game.Chess.recent_puzzles[rig.message.chat.id] == history
    assert isinstance(rig.session.methods[-1], SendMessage) and "Ошибка" in rig.session.methods[-1].text
    assert not reveals(rig)


async def test_fetch_render_and_send_share_one_deadline(rig, monkeypatch):
    monkeypatch.setattr(game, "PHOTO_TIMEOUT", 0.06)

    async def fetch(*args):
        await asyncio.sleep(0.025)
        return PUZZLE

    async def render(*args):
        await asyncio.sleep(0.025)
        return PNG

    async def photo(*args):
        await asyncio.sleep(0.025)

    game.random_puzzle.side_effect = fetch
    monkeypatch.setattr(game.asyncio, "to_thread", render)
    rig.session.photo_hook = photo
    await game.Chess.process(rig.message, rig.redis, rig.supervisor)
    assert not game.Chess.rounds and not game.Chess.recent_puzzles
    assert not any(isinstance(method, SendPhoto) for method in rig.session.methods)
    assert "Ошибка" in rig.session.methods[-1].text


@pytest.mark.parametrize("stage", ["fetch", "render", "send"])
async def test_start_errors_release_chat_for_retry(rig, stage):
    if stage == "fetch":
        game.random_puzzle.side_effect = ExternalServiceError("synthetic unavailable source")
    elif stage == "render":
        game.render_board.side_effect = ValueError("synthetic invalid board")
    else:

        async def fail(method):
            raise TelegramBadRequest(method=method, message="synthetic rejected photo")

        rig.session.photo_hook = fail
    await game.Chess.process(rig.message, rig.redis, rig.supervisor)
    assert not game.Chess.rounds and not game.Chess.recent_puzzles
    assert "Ошибка" in rig.session.methods[-1].text
    game.random_puzzle.side_effect = None
    game.render_board.side_effect = None
    rig.session.photo_hook = None
    assert (await start(rig)).message is not None


async def test_cancelled_start_releases_reserved_chat(rig):
    entered = asyncio.Event()

    async def fetch(*args):
        entered.set()
        await asyncio.Event().wait()

    game.random_puzzle.side_effect = fetch
    worker = asyncio.create_task(start(rig))
    await asyncio.wait_for(entered.wait(), timeout=1)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert not game.Chess.rounds and not game.Chess.recent_puzzles
    assert rig.supervisor.job_count == 0


async def test_cancelled_finish_callback_does_not_cancel_owned_completion(rig):
    round_ = await start(rig)
    await click(rig, round_, correct(round_))
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
        await rig.supervisor.drain(timeout=1, cancel_timeout=0.5)
        assert not game.Chess.rounds and len(reveals(rig)) == 1
        rig.client.eval.assert_awaited_once()
    finally:
        release.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_shutdown_cancels_timer_and_discards_only_memory(rig, monkeypatch):
    monkeypatch.setattr(game, "ROUND_TIMEOUT", 0.02)
    round_ = await start(rig)
    await game.Chess.shutdown()
    await asyncio.sleep(0.04)
    assert round_.timer.cancelled() and not game.Chess.rounds and not game.Chess.recent_puzzles
    assert not reveals(rig) and rig.supervisor.job_count == 0
    rig.client.eval.assert_not_awaited()


async def test_recent_history_is_last_fifteen_delivered_ids_per_chat(rig):
    ids = [f"id{index:03d}" for index in range(18)]
    for puzzle_id in ids:
        game.random_puzzle.return_value = replace(PUZZLE, id=puzzle_id)
        await game.Chess.send_round_photo(rig.message, game.Round("synthetic"))
    history = tuple(ids[-15:])
    assert game.Chess.recent_puzzles[rig.message.chat.id] == history
    other = make_message(rig.bot, chat={"id": -200, "type": "supergroup"})
    await game.Chess.send_round_photo(other, game.Round("other"))
    assert game.random_puzzle.call_args.args == ((),)
    assert game.Chess.recent_puzzles[rig.message.chat.id] == history
    await game.Chess.send_round_photo(rig.message, game.Round("next"))
    assert game.random_puzzle.call_args.args == (history,)
    assert game.Chess.recent_puzzles[rig.message.chat.id] == (*history, ids[-1])[-15:]


def test_today_changes_at_moscow_midnight(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 16, 21, 1, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(game, "datetime", Clock)
    assert REAL_TODAY() == DAY


async def test_score_adapter_daily_keys_deltas_and_retry_token_are_isolated_from_geoguess(rig):
    chat_id = rig.message.chat.id
    first = [(42, "Alice <name>", "alice", 1), (43, "Bob", None, -1)]
    await game.save_scores(chat_id, first, rig.redis, DAY, round_token="one")
    await game.save_scores(chat_id, first, rig.redis, DAY, round_token="one")
    key = game.score_key(chat_id, DAY)
    assert rig.client.scores[key] == {"42": 1, "43": 0}
    await game.save_scores(chat_id, [(42, "Alice", "alice", -1)], rig.redis, DAY, round_token="two")
    await game.save_scores(chat_id, [(42, "Alice", "alice", -1)], rig.redis, DAY, round_token="three")
    assert rig.client.scores[key]["42"] == 0
    tomorrow = date(2026, 9, 18)
    await game.save_scores(chat_id, first, rig.redis, tomorrow, round_token="one")
    assert rig.client.scores[game.score_key(chat_id, tomorrow)] == {"42": 1, "43": 0}
    assert key != geoguess.score_key(chat_id) and ":chess:" in key
    assert game.ChessCallback(round="one", choice="1").pack().startswith("chess:")
    assert game.ChessCallback(round="one", choice="1").pack() != geoguess.GeoguessCallback(round="one", choice="1").pack()
    assert rig.client.eval.call_args.args[2:6] == (
        game.score_key(chat_id, tomorrow),
        game.score_key(chat_id, tomorrow) + ":names",
        game.score_key(chat_id, tomorrow) + ":usernames",
        game.score_key(chat_id, tomorrow) + ":rounds",
    )
    assert all(expires > datetime.combine(DAY, datetime.min.time(), game.DAY_ZONE).timestamp() for expires in rig.client.expiries.values())


async def test_round_scores_on_finish_day_and_top_does_not_mix_midnight_keys(rig, monkeypatch):
    day = DAY
    monkeypatch.setattr(game, "today", lambda: day)
    round_ = await start(rig)
    await click(rig, round_, correct(round_))
    day = date(2026, 9, 18)
    await click(rig, round_, "finish")
    finish_day = day
    assert rig.client.scores[game.score_key(rig.message.chat.id, DAY)] == {}

    async def ranking(*args, **kwargs):
        nonlocal day
        day = date(2026, 9, 19)
        return [("42", 1)]

    rig.client.zrevrange.side_effect = ranking
    await game.Chess.top(rig.message, rig.redis)
    key = game.score_key(rig.message.chat.id, finish_day)
    rig.client.zrevrange.assert_awaited_once_with(key, 0, 9, withscores=True)
    assert all(call.args[0].startswith(key) for call in rig.client.hget.call_args_list)
    assert "18.09.2026" in rig.session.methods[-1].text and "@user_name" in rig.session.methods[-1].text


async def test_large_voter_lists_stay_in_one_photo_and_all_completed_pages_remain_accessible(rig):
    round_ = await start(rig)
    answer = correct(round_)
    round_.votes = {uid: (answer if uid % 2 else (answer + 1) % 6, f"Player {uid:04d} " + "<&🧭" * 60) for uid in range(150)}
    round_.usernames = {uid: f"participant_{uid:04d}" for uid in range(150)}
    await game.Chess.update_board(round_)
    active = await all_pages(rig, round_)
    hidden = "\n".join(page.caption for page in active)
    assert all(f"@participant_{uid:04d}" in hidden for uid in range(150))
    assert all(option.label not in hidden for option in round_.options)
    await click(rig, round_, "finish")
    assert not game.Chess.rounds and game.Chess.completed[rig.message.chat.id, round_.token] is round_
    final = await all_pages(rig, round_)
    shown = "\n".join(page.caption for page in final)
    assert all(f"@participant_{uid:04d}" in shown for uid in range(150))
    for view in [*active, *final]:
        units = len(view.caption.encode("utf-16-le")) // 2
        assert units <= 1024 and len(view.entities) < 100
        assert all(entity.offset + entity.length <= units for entity in view.entities)
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1
    assert not any(isinstance(method, (SendMessage, EditMessageText)) for method in rig.session.methods)
    assert len({method.message_id for method in captions(rig)}) == 1
    rig.client.eval.assert_awaited_once()


@pytest.mark.parametrize("caption_fails", [False, True])
async def test_failed_media_edit_uses_same_photo_and_can_retry_without_rescoring(rig, caption_fails):
    round_ = await start(rig)
    await click(rig, round_, correct(round_))
    rig.session.media_error = True
    rig.session.caption_error = caption_fails
    await click(rig, round_, "finish")
    result = rig.session.methods[-1]
    assert isinstance(result, EditMessageCaption) and result.message_id == round_.message.message_id
    assert all(move in result.caption for move in PUZZLE.line)
    assert result.parse_mode is None and not game.Chess.rounds
    assert not round_.solution_shown
    assert not any(isinstance(method, SendMessage) for method in rig.session.methods)
    assert round_.view.caption == result.caption if not caption_fails else round_.view.caption != result.caption
    rig.client.eval.assert_awaited_once()
    rig.session.media_error = rig.session.caption_error = False
    await click(rig, round_, "finish")
    assert round_.solution_shown and round_.closed
    assert len(reveals(rig)) == 2
    rig.client.eval.assert_awaited_once()


async def test_storage_failure_still_reveals_solution_and_top_reports_unavailable(rig):
    round_ = await start(rig)
    await click(rig, round_, (correct(round_) + 1) % 6)
    rig.client.eval.side_effect = RuntimeError("synthetic unavailable storage")
    await click(rig, round_, "finish")
    assert all(move in text_of(reveals(rig)[0]) for move in PUZZLE.line)
    assert "Не удалось подтвердить запись очков" in text_of(reveals(rig)[0])
    assert not game.Chess.rounds
    rig.client.zrevrange.side_effect = RuntimeError("synthetic unavailable storage")
    await game.Chess.top(rig.message, rig.redis)
    assert rig.session.methods[-1].text == "Рейтинг сейчас недоступен."


async def test_completion_persists_scores_before_waiting_for_display_lock(rig):
    round_ = await start(rig)
    await click(rig, round_, correct(round_))
    saved = asyncio.Event()
    original_apply = rig.client.apply

    async def save(*args):
        result = await original_apply(*args)
        saved.set()
        return result

    rig.client.eval.side_effect = save
    await round_.board_lock.acquire()
    try:
        task = game.Chess.start_finish(rig.message.chat.id, round_, rig.redis, rig.supervisor)
        await asyncio.wait_for(saved.wait(), timeout=1)
        assert rig.client.scores[game.score_key(rig.message.chat.id)] == {"42": 1}
        assert task is not None and not task.done()
        assert not reveals(rig)
    finally:
        round_.board_lock.release()
        await rig.supervisor.drain(timeout=1, cancel_timeout=0.5)
    assert len(reveals(rig)) == 1


async def test_concurrent_votes_coalesce_and_edit_attempts_are_paced(rig, monkeypatch):
    monkeypatch.setattr(game, "EDIT_INTERVAL", 0.03)
    round_ = await start(rig)
    edit_times = []

    async def note_edit(method):
        edit_times.append(asyncio.get_running_loop().time())

    rig.session.edit_hook = note_edit
    await asyncio.gather(*(click(rig, round_, correct(round_), user_id=uid) for uid in range(24)))
    assert len(round_.votes) == 24 and len(captions(rig)) <= 2
    assert "24" in round_.view.caption
    await click(rig, round_, "page_1")
    await click(rig, round_, "finish")
    assert all(later - earlier >= 0.025 for earlier, later in zip(edit_times, edit_times[1:]))
    assert round_.closed and round_.solution_shown
    assert len(reveals(rig)) == 1


@pytest.mark.parametrize("finish", [False, True])
async def test_not_modified_response_accepts_already_delivered_view(rig, finish):
    round_ = await start(rig)

    async def not_modified(method):
        raise TelegramBadRequest(method=method, message="Bad Request: message is not modified: content and reply markup are the same")

    rig.session.edit_hook = not_modified
    await click(rig, round_, "finish" if finish else correct(round_))
    assert round_.view == game.Chess.render_view(round_)
    assert round_.solution_shown is finish
    count = len(captions(rig))
    await game.Chess.update_board(round_)
    assert len(captions(rig)) == count


async def test_render_failure_keeps_solution_readable_and_retryable(rig):
    round_ = await start(rig)
    game.render_board.side_effect = ValueError("Synthetic unavailable renderer")
    await click(rig, round_, "finish")
    assert not round_.solution_shown and not reveals(rig)
    assert all(move in round_.view.caption for move in PUZZLE.line)
    assert isinstance(captions(rig)[-1], EditMessageCaption)
    game.render_board.side_effect = None
    await click(rig, round_, "finish")
    assert round_.solution_shown and len(reveals(rig)) == 1
    assert not any(isinstance(method, SendMessage) for method in rig.session.methods)


@pytest.mark.parametrize("choice", ["page_-1", "page_", "page_1.5", "page_١", "page_999999999"])
async def test_invalid_page_buttons_do_not_change_votes_or_display(rig, choice):
    round_ = await start(rig)
    view = round_.view
    await click(rig, round_, choice)
    assert round_.view == view and not round_.votes and not captions(rig)
    assert rig.session.methods[-1].text == "Неизвестная страница."


async def test_completed_navigation_does_not_interfere_with_new_game_or_repeat_scoring(rig):
    old = await start(rig)
    await click(rig, old, correct(old))
    await click(rig, old, "finish")
    current = await start(rig)
    await click(rig, old, "page_999")
    assert old.view.page == old.view.pages - 1
    assert current is game.Chess.rounds[rig.message.chat.id] and not current.closed and not current.votes
    rig.client.eval.assert_awaited_once()
    game.Chess.completed.clear()
    before = len(captions(rig))
    await click(rig, old, "page_0")
    assert len(captions(rig)) == before + 1
    restored = game.Chess.completed[rig.message.chat.id, old.token]
    assert restored is not old and restored.closed and restored.scored
    assert restored.message.message_id == old.message.message_id
    assert game.Chess.rounds[rig.message.chat.id] is current
    rig.client.eval.assert_awaited_once()
    assert game.Chess.completed.maxsize == 128 and game.Chess.completed.ttl == 86400


async def test_send_deadline_includes_blocked_telegram_middleware(rig, monkeypatch):
    monkeypatch.setattr(game, "SEND_TIMEOUT", 0.01)
    cancelled = asyncio.Event()

    async def blocked(make_request, bot, method):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    rig.session.middleware(blocked)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(game._send(rig.message.reply("synthetic")), timeout=0.5)
    assert cancelled.is_set() and not rig.session.methods


def snapshot(rig, round_):
    key = ("chess", rig.bot.id, rig.message.chat.id, round_.token)
    return game.quiz_store.SavedRound.model_validate_json(rig.snapshots.states[key])


async def settle(round_):
    if round_.task is not None:
        await asyncio.wait_for(asyncio.shield(round_.task), timeout=1)


async def test_delivered_round_and_acknowledged_vote_are_saved_with_original_message(rig):
    before = game.wall_time()
    round_ = await start(rig)
    initial = snapshot(rig, round_)
    assert before + 600 <= initial.deadline <= game.wall_time() + 600
    assert initial.message.message_id == round_.message.message_id and initial.message.message_thread_id == 17
    assert game.PUZZLE_ADAPTER.validate_json(initial.question) == PUZZLE
    await click(rig, round_, correct(round_))
    accepted = snapshot(rig, round_)
    assert accepted.votes == round_.votes and accepted.usernames == {42: "user_name"}
    assert not accepted.closed and not accepted.revealed


async def test_initial_save_failure_keeps_delivered_card_and_deadline(rig):
    rig.snapshots.save.side_effect = RuntimeError("Unavailable snapshots")
    round_ = await start(rig)
    assert round_.message is not None and round_.deadline > game.wall_time()
    assert round_.timer is not None and not round_.timer.cancelled()
    assert not any(isinstance(method, SendMessage) for method in rig.session.methods)
    rig.snapshots.save.side_effect = rig.snapshots.put
    await click(rig, round_, correct(round_))
    assert snapshot(rig, round_).votes == round_.votes


async def test_lost_vote_save_reply_preserves_choice_and_repeated_click_confirms_it(rig):
    round_ = await start(rig)
    answer = correct(round_)

    async def commit_then_fail(*args):
        await rig.snapshots.put(*args)
        raise TimeoutError("Reply lost after commit")

    rig.snapshots.save.side_effect = commit_then_fail
    await click(rig, round_, answer)
    assert round_.votes[42][0] == answer and snapshot(rig, round_).votes[42][0] == answer
    assert "Не удалось подтвердить" in rig.session.methods[-1].text
    rig.snapshots.save.side_effect = rig.snapshots.put
    await click(rig, round_, (answer + 1) % 6)
    assert "уже принят" in rig.session.methods[-1].text
    assert round_.votes[42][0] == snapshot(rig, round_).votes[42][0] == answer
    await click(rig, round_, "finish")
    assert rig.client.scores[game.score_key(rig.message.chat.id, round_.score_day)] == {"42": 1}


async def test_cancelled_vote_after_commit_does_not_erase_the_durable_choice(rig):
    round_ = await start(rig)
    committed = asyncio.Event()

    async def commit_and_block(*args):
        await rig.snapshots.put(*args)
        committed.set()
        await asyncio.Event().wait()

    rig.snapshots.save.side_effect = commit_and_block
    worker = asyncio.create_task(click(rig, round_, correct(round_)))
    await committed.wait()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert round_.votes == snapshot(rig, round_).votes
    rig.snapshots.save.side_effect = rig.snapshots.put
    await click(rig, round_, correct(round_))
    assert "уже принят" in rig.session.methods[-1].text


async def test_restart_restores_remaining_time_votes_and_same_card(rig, monkeypatch):
    now = game.wall_time()
    monkeypatch.setattr(game, "wall_time", lambda: now)
    old = await start(rig)
    await click(rig, old, correct(old))
    deadline = old.deadline
    await game.Chess.shutdown()
    now += 400
    await game.Chess.restore(rig.bot, rig.redis, rig.supervisor)
    recovered = game.Chess.rounds[rig.message.chat.id]
    assert recovered is not old and recovered.deadline == deadline
    assert recovered.votes == old.votes and recovered.message.message_id == old.message.message_id
    assert recovered.timer.when() - asyncio.get_running_loop().time() == pytest.approx(200, abs=0.1)
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1
    await click(rig, recovered, "finish")
    assert recovered.revealed and snapshot(rig, recovered).revealed


async def test_restart_finishes_overdue_round_on_deadline_day(rig):
    old = await start(rig)
    await click(rig, old, correct(old))
    old.deadline = game.wall_time() - 1
    await game.Chess.persist(old, rig.redis)
    expected_day = datetime.fromtimestamp(old.deadline, game.DAY_ZONE).date()
    await game.Chess.shutdown()
    await game.Chess.restore(rig.bot, rig.redis, rig.supervisor)
    recovered = game.Chess.rounds[rig.message.chat.id]
    await settle(recovered)
    assert recovered.closed and recovered.revealed and recovered.score_day == expected_day
    assert rig.client.scores[game.score_key(rig.message.chat.id, expected_day)] == {"42": 1}
    assert not game.Chess.rounds


async def test_overdue_callback_cannot_accept_late_vote_when_timer_has_not_run(rig):
    round_ = await start(rig)
    round_.timer.cancel()
    round_.deadline = game.wall_time() - 1
    await click(rig, round_, correct(round_))
    assert not round_.votes and round_.closed
    await settle(round_)
    assert round_.revealed and not game.Chess.rounds


async def test_closure_is_durable_before_scoring_and_new_round_is_not_blocked_by_edit_lock(rig, monkeypatch):
    monkeypatch.setattr(game, "UPDATE_TIMEOUT", 0.02)
    round_ = await start(rig)
    await click(rig, round_, correct(round_))
    applied = rig.client.apply

    async def check_saved_closure(*args):
        saved = snapshot(rig, round_)
        assert saved.closed and saved.score_day == DAY
        return await applied(*args)

    rig.client.eval.side_effect = check_saved_closure
    await round_.board_lock.acquire()
    try:
        await asyncio.wait_for(click(rig, round_, "finish"), timeout=0.2)
        assert round_.scored and not round_.revealed and not game.Chess.rounds
        assert round_.timer is not None and not round_.timer.cancelled()
        current = await start(rig)
        assert current is not round_ and not current.closed
    finally:
        round_.board_lock.release()
    task = game.Chess.start_finish(rig.message.chat.id, round_, rig.redis, rig.supervisor)
    assert task is not None
    await settle(round_)
    assert round_.revealed and game.Chess.rounds[rig.message.chat.id] is current
    rig.client.eval.assert_awaited_once()


async def test_failed_result_delivery_retries_automatically_without_a_click(rig, monkeypatch):
    monkeypatch.setattr(game, "RETRY_INTERVAL", 0.02)
    round_ = await start(rig)
    await click(rig, round_, correct(round_))
    rig.session.media_error = rig.session.caption_error = True
    await click(rig, round_, "finish")
    assert not round_.revealed and round_.scored and not game.Chess.rounds
    delivered = asyncio.Event()

    async def notice(method):
        delivered.set()

    rig.session.edit_hook = notice
    rig.session.media_error = rig.session.caption_error = False
    await asyncio.wait_for(delivered.wait(), timeout=1)
    await settle(round_)
    assert round_.revealed and snapshot(rig, round_).revealed
    rig.client.eval.assert_awaited_once()


async def test_score_retry_after_midnight_uses_frozen_day_and_rewrites_error_result(rig, monkeypatch):
    monkeypatch.setattr(game, "RETRY_INTERVAL", 0.02)
    day = DAY
    monkeypatch.setattr(game, "today", lambda: day)
    round_ = await start(rig)
    await click(rig, round_, correct(round_))
    rig.client.eval.side_effect = RuntimeError("Scores unavailable")
    await click(rig, round_, "finish")
    assert round_.scored is False and round_.revealed and round_.score_day == DAY
    day = date(2026, 9, 18)
    applied = asyncio.Event()

    async def score(*args):
        value = await rig.client.apply(*args)
        applied.set()
        return value

    rig.client.eval.side_effect = score
    await asyncio.wait_for(applied.wait(), timeout=1)
    await settle(round_)
    assert round_.scored and round_.revealed and snapshot(rig, round_).score_day == DAY
    assert rig.client.scores[game.score_key(rig.message.chat.id, DAY)] == {"42": 1}
    assert rig.client.scores[game.score_key(rig.message.chat.id, day)] == {}
    assert "Не удалось подтвердить" not in round_.view.caption


async def test_failed_final_snapshot_retries_without_applying_scores_twice(rig, monkeypatch):
    monkeypatch.setattr(game, "RETRY_INTERVAL", 0.02)
    round_ = await start(rig)
    await click(rig, round_, correct(round_))
    lost = False
    persisted = asyncio.Event()

    async def lose_one_final_reply(kind, state, redis):
        nonlocal lost
        if state.closed and state.scored and state.revealed:
            if not lost:
                lost = True
                raise TimeoutError("Final save lost")
            await rig.snapshots.put(kind, state, redis)
            persisted.set()
        else:
            await rig.snapshots.put(kind, state, redis)

    rig.snapshots.save.side_effect = lose_one_final_reply
    await click(rig, round_, "finish")
    assert lost and round_.scored and not round_.revealed and not round_.timer.cancelled()
    await asyncio.wait_for(persisted.wait(), timeout=1)
    await settle(round_)
    assert round_.revealed and snapshot(rig, round_).revealed
    rig.client.eval.assert_awaited_once()


async def test_concurrent_lazy_loads_share_one_round_and_keep_both_votes(rig):
    old = await start(rig)
    await game.Chess.shutdown()
    loaded = 0
    both = asyncio.Event()

    async def load(*args):
        nonlocal loaded
        state = await rig.snapshots.get(*args)
        loaded += 1
        if loaded == 2:
            both.set()
        await both.wait()
        return state

    rig.snapshots.load.side_effect = load
    await asyncio.gather(click(rig, old, correct(old), user_id=42), click(rig, old, correct(old), user_id=43))
    recovered = game.Chess.rounds[rig.message.chat.id]
    assert recovered is not old and set(recovered.votes) == {42, 43}
    assert snapshot(rig, recovered).votes == recovered.votes


async def test_shutdown_cancels_retry_timer_without_discarding_saved_failure(rig, monkeypatch):
    monkeypatch.setattr(game, "RETRY_INTERVAL", 0.02)
    round_ = await start(rig)
    await click(rig, round_, correct(round_))
    rig.client.eval.side_effect = RuntimeError("Scores unavailable")
    await click(rig, round_, "finish")
    retry = round_.timer
    assert not retry.cancelled()
    await game.Chess.shutdown()
    await asyncio.sleep(0.04)
    assert retry.cancelled() and not game.Chess.completed
    rig.client.eval.assert_awaited_once()
    assert snapshot(rig, round_).closed and snapshot(rig, round_).scored is False


async def test_restore_old_active_snapshot_does_not_replace_a_newer_game(rig):
    old = await start(rig)
    old.timer.cancel()
    game.Chess.rounds.clear()
    current = await start(rig)
    await game.Chess.restore(rig.bot, rig.redis, rig.supervisor)
    recovered_old = game.Chess.completed[rig.message.chat.id, old.token]
    await settle(recovered_old)
    assert recovered_old.closed and game.Chess.rounds[rig.message.chat.id] is current
    assert not current.closed and not current.timer.cancelled()


async def test_retry_stops_at_snapshot_retention_boundary(rig):
    round_ = await start(rig)
    round_.closed = True
    round_.scored = False
    round_.deadline = game.wall_time() - game.RESULT_TTL + 10
    timer = round_.timer
    game.Chess.arm_timer(rig.message.chat.id, round_, rig.redis, rig.supervisor)
    assert timer.cancelled()


async def test_slow_duplicate_acknowledgement_does_not_hold_state_lock_or_block_completion(rig, monkeypatch):
    round_ = await start(rig)
    await click(rig, round_, correct(round_))
    blocked, release = asyncio.Event(), asyncio.Event()
    send = game._send

    async def delay_duplicate(method):
        if isinstance(method, AnswerCallbackQuery) and "уже принят" in (method.text or ""):
            blocked.set()
            await release.wait()
        return await send(method)

    monkeypatch.setattr(game, "_send", delay_duplicate)
    duplicate = asyncio.create_task(click(rig, round_, correct(round_)))
    try:
        await asyncio.wait_for(blocked.wait(), timeout=1)
        game.Chess.start_finish(rig.message.chat.id, round_, rig.redis, rig.supervisor)
        await settle(round_)
        assert round_.scored and round_.revealed and not game.Chess.rounds
        assert not duplicate.done()
    finally:
        release.set()
        await duplicate


async def test_restart_retries_closed_unscored_round_with_its_original_day(rig, monkeypatch):
    old = await start(rig)
    await click(rig, old, correct(old))
    rig.client.eval.side_effect = RuntimeError("Scores unavailable")
    await click(rig, old, "finish")
    assert snapshot(rig, old).closed and snapshot(rig, old).score_day == DAY
    await game.Chess.shutdown()
    monkeypatch.setattr(game, "today", lambda: date(2026, 9, 18))
    rig.client.eval.side_effect = rig.client.apply
    await game.Chess.restore(rig.bot, rig.redis, rig.supervisor)
    recovered = game.Chess.completed[rig.message.chat.id, old.token]
    await settle(recovered)
    assert recovered.score_day == DAY and recovered.scored and recovered.revealed
    assert rig.client.scores[game.score_key(rig.message.chat.id, DAY)] == {"42": 1}
    assert snapshot(rig, recovered).revealed
