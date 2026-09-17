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
async def rig(monkeypatch):
    monkeypatch.setattr(game.Chess, "rounds", {})
    monkeypatch.setattr(game.Chess, "recent_puzzles", game.LRUCache(maxsize=1024))
    monkeypatch.setattr(game, "today", lambda: DAY)
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
    hidden = "\n".join(round_.board_texts)
    assert "2" in hidden and "User &lt;name&gt;" in hidden and "@user_name" in hidden
    assert "tg://user?id=43" in hidden
    assert all(option.label not in hidden for option in round_.options)
    assert round_.votes[42][0] == answer
    await asyncio.gather(click(rig, round_, "finish", user_id=99), click(rig, round_, "finish", user_id=98))
    await click(rig, round_, answer, user_id=44)
    assert 44 not in round_.votes and not game.Chess.rounds
    rig.client.eval.assert_awaited_once()
    assert rig.client.scores[game.score_key(rig.message.chat.id)] == {"42": 1, "43": 0}
    deltas = rig.client.eval.call_args.args[8:]
    assert deltas == ("42", "User <name>", "user_name", 1, "43", "User <name>", "", -1)
    final = "\n".join(round_.board_texts)
    assert all(option.label in final for option in round_.options)
    assert "✅" in final and "❌" in final and "@user_name" in final and "tg://user?id=43" in final
    assert len(reveals(rig)) == 1 and reveals(rig)[0].reply_markup is None
    assert all(move in text_of(reveals(rig)[0]) for move in PUZZLE.line)
    assert PUZZLE.id in text_of(reveals(rig)[0])
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


async def test_large_voter_lists_escape_html_and_keep_everyone_in_safe_chunks(rig):
    round_ = await start(rig)
    answer = correct(round_)
    round_.votes = {uid: (answer if uid % 2 else (answer + 1) % 6, f"Player {uid:04d} " + "<&" * 60) for uid in range(150)}
    round_.usernames = {uid: f"participant_{uid:04d}" for uid in range(150)}
    await game.Chess.update_board(round_)
    assert len(round_.board_texts) > 1
    hidden = "\n".join(round_.board_texts)
    assert all(f"@participant_{uid:04d}" in hidden for uid in range(150))
    assert all(option.label not in hidden for option in round_.options)
    await click(rig, round_, "finish")
    shown = "\n".join(round_.board_texts)
    assert all(f"@participant_{uid:04d}" in shown for uid in range(150))
    assert "<&" not in shown and "&lt;&amp;" in shown
    assert all(len(text) <= 3000 for text in round_.board_texts)
    assert all(len(text_of(method)) <= 4096 for method in rig.session.methods if isinstance(method, (SendMessage, EditMessageText)))
    assert all(len(text_of(method)) <= 1024 for method in rig.session.methods if isinstance(method, (SendPhoto, EditMessageMedia)))


@pytest.mark.parametrize("caption_fails", [False, True])
async def test_failed_media_edit_still_delivers_solution_in_same_topic(rig, caption_fails):
    round_ = await start(rig)
    rig.session.media_error = True
    rig.session.caption_error = caption_fails
    await click(rig, round_, "finish")
    result = rig.session.methods[-1]
    assert all(move in text_of(result) for move in PUZZLE.line)
    if caption_fails:
        assert isinstance(result, SendMessage)
        assert result.reply_parameters.message_id == round_.message.message_id and result.message_thread_id == 17
    else:
        assert isinstance(result, EditMessageCaption) and result.message_id == round_.message.message_id
        assert result.reply_markup is None
    assert result.parse_mode == "HTML" and not game.Chess.rounds


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
