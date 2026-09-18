"""Public chess matches through real aiogram messages and offline service boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageCaption, EditMessageMedia, EditMessageText, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, User

from msu_hub_bot.commands import chess_play as command
from msu_hub_bot.commands.chess_play_view import PlayCallback
from msu_hub_bot.telegram.chess_play_store import Conflict
from msu_hub_bot.telegram.runtime import Supervisor
from telegram_helpers import RecordingSession, make_bot, make_message


PNG = b"\x89PNG\r\n\x1a\nsynthetic-chess-board"
WHITE = User(id=42, is_bot=False, first_name="Белый", username="white")
BLACK = User(id=43, is_bot=False, first_name="Чёрный", username="black")
VIEWER = User(id=44, is_bot=False, first_name="Зритель", username="viewer")


class BoardSession(RecordingSession):
    def __init__(self):
        super().__init__()
        self.messages = {}
        self.timeouts = []
        self.edit_hook = None

    async def make_request(self, bot, method, timeout=None):
        self.timeouts.append(timeout)
        if isinstance(method, (SendPhoto, SendMessage)):
            self.methods.append(method)
            result = make_message(
                bot,
                message_id=100 + len(self.messages),
                chat={"id": method.chat_id, "type": "supergroup"},
                message_thread_id=method.message_thread_id,
                is_topic_message=method.message_thread_id is not None,
            )
            self.messages[result.message_id] = result
            return result
        if isinstance(method, (EditMessageMedia, EditMessageCaption, EditMessageText)):
            self.methods.append(method)
            if self.edit_hook is not None:
                await self.edit_hook(method)
            return self.messages[method.message_id]
        return await super().make_request(bot, method, timeout)


class MemoryMatches:
    """Copying compare-and-set snapshots model the storage adapter's public contract."""

    def __init__(self):
        self.values = {}
        self.active_tokens = {}
        self.fail_after_save = False
        self.finished = asyncio.Event()

    async def save(self, redis, value, previous=None):
        await asyncio.sleep(0)
        g = value.game
        key, active_key = (g.bot_id, g.chat_id, g.token), (g.bot_id, g.chat_id)
        current = self.values.get(key)
        if current == value:
            return
        if previous is None:
            if current is not None or active_key in self.active_tokens:
                raise Conflict("occupied")
        elif current != previous:
            raise Conflict("changed")
        self.values[key] = value.model_copy(deep=True)
        if g.status == "finished":
            self.finished.set()
            if self.active_tokens.get(active_key) == g.token:
                self.active_tokens.pop(active_key)
        else:
            self.active_tokens[active_key] = g.token
        if self.fail_after_save:
            self.fail_after_save = False
            raise OSError("Synthetic response lost after durable save")

    async def load(self, redis, bot_id, chat_id, token):
        await asyncio.sleep(0)
        value = self.values.get((bot_id, chat_id, token))
        return value.model_copy(deep=True) if value is not None else None

    async def active(self, redis, bot_id, chat_id):
        token = self.active_tokens.get((bot_id, chat_id))
        return await self.load(redis, bot_id, chat_id, token) if token else None

    async def pending(self, redis, bot_id):
        return [value.model_copy(deep=True) for key, value in self.values.items() if key[0] == bot_id and not value.revealed]


class MemoryRatings:
    def __init__(self):
        self.scores = {}
        self.settlements = {}
        self.pairs = []

    async def get_player(self, bot_id, user_id, redis):
        return command.rating.RatedPlayer(user_id=user_id, name="Игрок", rating=self.scores.get(user_id, 800))

    async def start_pair(self, bot_id, white, black, redis):
        await asyncio.sleep(0)
        self.pairs.append((white.user_id, black.user_id))
        return self.scores.setdefault(white.user_id, 800), self.scores.setdefault(black.user_id, 800)

    async def settle(self, game, redis):
        key = (game.bot_id, game.chat_id, game.token)
        if key in self.settlements:
            return self.settlements[key]
        black = game.black
        assert black is not None
        score = 0.5 if game.winner is None else float(game.winner == game.white.user_id)
        delta = command.rating.elo_delta(game.white_rating, game.black_rating, score)
        white_before, black_before = self.scores[game.white.user_id], self.scores[black.user_id]
        self.scores[game.white.user_id] += delta
        self.scores[black.user_id] -= delta
        result = ((white_before, self.scores[game.white.user_id]), (black_before, self.scores[black.user_id]))
        self.settlements[key] = result
        return result


@pytest.fixture
async def rig(monkeypatch):
    await command.ChessPlay.close()
    command.ChessPlay.open()
    bot = make_bot()
    await bot.session.close()
    session = BoardSession()
    bot.session = session
    matches, ratings = MemoryMatches(), MemoryRatings()
    now = [1000.0]
    render = Mock(return_value=PNG)
    monkeypatch.setattr(command, "time", lambda: now[0])
    monkeypatch.setattr(command, "CLOCK_INTERVAL", 3600)
    monkeypatch.setattr(command, "render_match", render)
    for name in ("save", "load", "active", "pending"):
        monkeypatch.setattr(command.store, name, getattr(matches, name))
    monkeypatch.setattr(command.rating, "get_player", ratings.get_player)
    monkeypatch.setattr(command.rating, "start_pair", ratings.start_pair)
    monkeypatch.setattr(command.rating, "settle", ratings.settle)
    state = SimpleNamespace(
        bot=bot, session=session, store=matches, ratings=ratings, now=now, render=render, redis=object(), supervisor=Supervisor()
    )
    try:
        yield state
    finally:
        await command.ChessPlay.close()
        await state.supervisor.drain(timeout=1, cancel_timeout=0.5)
        await bot.session.close()


async def quiet(rig):
    for _ in range(20):
        tasks = [
            task
            for match in command.ChessPlay.matches.values()
            for task in (match.maintenance, match.delivery)
            if task is not None and not task.done()
        ]
        if not tasks:
            return
        for match in command.ChessPlay.matches.values():
            match.last_edit = 0
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
    raise AssertionError("Chess jobs did not settle")


async def create(rig, *, thread_id=None, chat_id=-1001234567890):
    message = make_message(
        rig.bot,
        from_user=WHITE,
        text="/chess_play",
        chat={"id": chat_id, "type": "supergroup"},
        message_thread_id=thread_id,
        is_topic_message=thread_id is not None,
    )
    await command.ChessPlay.process(message, rig.redis, rig.supervisor)
    match = next(value for value in command.ChessPlay.matches.values() if value.saved.game.chat_id == chat_id)
    match.last_edit = 0
    await quiet(rig)
    return match


async def tap(rig, match, action, value="", *, user=WHITE, revision=None, settle=True):
    game = match.saved.game
    data = PlayCallback(game=game.token, revision=game.revision if revision is None else revision, action=action, value=value)
    query = CallbackQuery(
        id="synthetic", chat_instance="synthetic", from_user=user, message=rig.session.messages[game.message_id], data=data.pack()
    ).as_(rig.bot)
    match.last_edit = 0
    await command.ChessPlay.callback(query, data, rig.redis, rig.supervisor)
    if settle:
        await quiet(rig)
    return [method for method in rig.session.methods if isinstance(method, AnswerCallbackQuery)][-1]


async def started(rig):
    match = await create(rig)
    await tap(rig, match, "join", user=BLACK)
    return match


async def test_invitation_is_public_with_creator_white_and_join_button(rig):
    match = await create(rig, thread_id=55)
    sent = next(method for method in rig.session.methods if isinstance(method, SendPhoto))
    assert sent.chat_id == match.saved.game.chat_id and sent.message_thread_id == 55
    assert sent.photo.data == PNG
    assert sent.reply_markup is None  # Buttons wait for the durable message id.
    controls = [method for method in rig.session.methods if isinstance(method, EditMessageCaption)][-1]
    assert "@white" in controls.reply_markup.inline_keyboard[0][0].text
    assert "10:00" in sent.caption
    assert match.saved.game.white.user_id == WHITE.id
    assert match.saved.game.black is None
    assert match.deadline_timer is not None
    assert rig.session.timeouts and set(rig.session.timeouts) == {command.SEND_TIMEOUT}


async def test_only_first_other_participant_can_join(rig):
    match = await create(rig)
    self_join = await tap(rig, match, "join")
    assert "другой участник" in self_join.text
    assert match.saved.game.black is None
    await asyncio.gather(
        tap(rig, match, "join", user=BLACK, revision=0, settle=False), tap(rig, match, "join", user=VIEWER, revision=0, settle=False)
    )
    await quiet(rig)
    assert match.saved.game.black.user_id in {BLACK.id, VIEWER.id}
    assert match.saved.game.white.user_id == WHITE.id
    assert len(rig.ratings.pairs) == 1
    assert match.saved.game.revision == 1


async def test_one_active_match_per_chat_across_forum_topics(rig):
    match = await create(rig, thread_id=1)
    await create(rig, thread_id=2)
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1
    assert match.saved.game.thread_id == 1
    assert "текущая партия" in next(method for method in rig.session.methods if isinstance(method, SendMessage)).text


async def test_cancelled_invitation_releases_chat_for_new_match(rig):
    first = await create(rig)
    await tap(rig, first, "cancel")
    assert first.saved.game.status == "finished"
    second = await create(rig)
    assert second.saved.game.token != first.saved.game.token
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 2


async def test_spectators_and_out_of_turn_player_cannot_change_selection_or_move(rig):
    match = await started(rig)
    await tap(rig, match, "pick", "e2")
    before = match.saved.game.model_dump_json()
    for user in (BLACK, VIEWER):
        reply = await tap(rig, match, "pick", "g1", user=user)
        assert "ход соперника" in reply.text if user is BLACK else "наблюдаете" in reply.text
        assert match.saved.game.model_dump_json() == before
    await tap(rig, match, "move", "e2e4", user=VIEWER)
    assert match.saved.game.model_dump_json() == before


async def test_moves_update_photo_but_selection_and_clock_refresh_only_caption(rig):
    match = await started(rig)
    before = rig.render.call_count
    media_before = sum(isinstance(method, EditMessageMedia) for method in rig.session.methods)
    await tap(rig, match, "pick", "e2")
    assert rig.render.call_count == before
    rig.now[0] += 20
    await tap(rig, match, "move", "e2e4")
    assert match.saved.game.remaining(rig.now[0]) == (585, 600)
    assert sum(isinstance(method, EditMessageMedia) for method in rig.session.methods) == media_before + 1
    assert rig.render.call_count == before + 1
    rig.now[0] += 7
    match.last_edit = 0
    command.ChessPlay._refresh(match)
    await quiet(rig)
    assert rig.render.call_count == before + 1
    captions = [method for method in rig.session.methods if isinstance(method, EditMessageCaption)]
    assert "09:53" in captions[-1].caption


async def test_duplicate_and_stale_callbacks_never_replay_move_or_increment(rig):
    match = await started(rig)
    revision = match.saved.game.revision
    rig.now[0] += 10
    await tap(rig, match, "move", "e2e4", revision=revision)
    before = match.saved.game.model_dump_json()
    reply = await tap(rig, match, "move", "e2e4", revision=revision)
    assert "обновилась" in reply.text
    assert match.saved.game.model_dump_json() == before
    assert match.saved.game.moves == ["e2e4"]
    assert match.saved.game.white_seconds == 595


async def test_expired_clock_rejects_move_at_exact_deadline_and_shows_winner(rig):
    match = await started(rig)
    rig.now[0] = match.saved.game.deadline()
    reply = await tap(rig, match, "move", "e2e4")
    assert "завершена" in reply.text
    assert match.saved.game.result == "timeout"
    assert match.saved.game.winner == BLACK.id
    assert match.saved.game.moves == []
    assert match.saved.ratings == ((800, 784), (800, 816))
    assert len(rig.ratings.settlements) == 1
    edits = [method for method in rig.session.methods if isinstance(method, EditMessageMedia)]
    assert "Победитель:" in edits[-1].media.caption and "@black" in edits[-1].media.caption
    assert await rig.store.active(rig.redis, rig.bot.id, match.saved.game.chat_id) is None
    await create(rig)
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 2


async def test_deadline_adjudication_does_not_wait_for_blocked_telegram_edit(rig):
    match = await started(rig)
    editing, release = asyncio.Event(), asyncio.Event()

    async def block_edit(method):
        editing.set()
        await release.wait()

    rig.session.edit_hook = block_edit
    match.last_edit = 0
    command.ChessPlay._show(match)
    await asyncio.wait_for(editing.wait(), 1)
    try:
        rig.now[0] = match.saved.game.deadline()
        command.ChessPlay._wake(match)
        await asyncio.wait_for(match.maintenance, 1)
        assert match.saved.game.winner == BLACK.id
        assert match.saved.game.status == "finished"
        assert await rig.store.active(rig.redis, rig.bot.id, match.saved.game.chat_id) is None
    finally:
        release.set()
        await quiet(rig)
    assert match.saved.revealed


async def test_restart_restores_current_turn_clocks_and_existing_message(rig):
    match = await started(rig)
    rig.now[0] += 30
    await tap(rig, match, "move", "e2e4")
    game = match.saved.game
    deadline = game.deadline()
    message_id = game.message_id
    await command.ChessPlay.close()
    rig.now[0] += 90
    command.ChessPlay.open()
    await command.ChessPlay.restore(rig.bot, rig.redis, rig.supervisor)
    await quiet(rig)
    restored = next(iter(command.ChessPlay.matches.values()))
    assert restored.saved.game.message_id == message_id
    assert restored.saved.game.deadline() == deadline
    assert restored.saved.game.remaining(rig.now[0]) == (575, 510)
    assert restored.saved.game.turn_player.user_id == BLACK.id
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1


async def test_restart_after_deadline_finishes_once_and_keeps_global_rating(rig):
    match = await started(rig)
    deadline, token = match.saved.game.deadline(), match.saved.game.token
    await command.ChessPlay.close()
    rig.now[0] = deadline + 20
    command.ChessPlay.open()
    await command.ChessPlay.restore(rig.bot, rig.redis, rig.supervisor)
    await quiet(rig)
    saved = await rig.store.load(rig.redis, rig.bot.id, match.saved.game.chat_id, token)
    assert saved.game.winner == BLACK.id and saved.revealed
    assert rig.ratings.scores == {WHITE.id: 784, BLACK.id: 816}
    await command.ChessPlay.restore(rig.bot, rig.redis, rig.supervisor)
    await quiet(rig)
    assert len(rig.ratings.settlements) == 1


async def test_ambiguous_durable_move_response_reloads_without_duplicate_increment(rig):
    match = await started(rig)
    revision = match.saved.game.revision
    rig.now[0] += 10
    rig.store.fail_after_save = True
    await tap(rig, match, "move", "e2e4", revision=revision)
    await tap(rig, match, "move", "e2e4", revision=revision)
    assert match.saved.game.moves == ["e2e4"]
    assert match.saved.game.white_seconds == 595


async def test_actual_deadline_timer_finishes_without_any_button_press(rig, monkeypatch):
    match = await started(rig)
    value = match.saved.model_copy(deep=True)
    value.game.white_seconds = 0.025
    await command.ChessPlay._commit(match, value)
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    monkeypatch.setattr(command, "time", lambda: rig.now[0] + loop.time() - started_at)
    command.ChessPlay._arm(match)
    await asyncio.wait_for(rig.store.finished.wait(), 1)
    assert loop.time() - started_at < 0.5
    assert match.saved.game.result == "timeout" and match.saved.game.winner == BLACK.id
    await quiet(rig)


async def test_second_command_during_initial_save_cannot_cancel_creator(rig, monkeypatch):
    stored, release = asyncio.Event(), asyncio.Event()
    original = rig.store.save
    first = True

    async def blocked_first_save(redis, value, previous=None):
        nonlocal first
        await original(redis, value, previous)
        if first:
            first = False
            stored.set()
            await release.wait()

    monkeypatch.setattr(command.store, "save", blocked_first_save)
    message = make_message(rig.bot, from_user=WHITE, text="/chess_play", message_thread_id=1, is_topic_message=True)
    first_task = asyncio.create_task(command.ChessPlay.process(message, rig.redis, rig.supervisor))
    await asyncio.wait_for(stored.wait(), 1)
    other = make_message(rig.bot, from_user=VIEWER, text="/chess_play", message_thread_id=2, is_topic_message=True)
    second_task = asyncio.create_task(command.ChessPlay.process(other, rig.redis, rig.supervisor))
    try:
        await asyncio.sleep(0.01)
        assert not any(value.game.status == "finished" for value in rig.store.values.values())
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), 1)
    await quiet(rig)
    active = await rig.store.active(rig.redis, rig.bot.id, message.chat.id)
    assert active is not None and active.game.white.user_id == WHITE.id
    assert active.game.thread_id == 1 and active.game.status == "waiting"
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1


async def test_photo_metadata_failure_recovers_same_board_without_duplicate_upload(rig, monkeypatch):
    original = rig.store.save
    attempts = 0

    async def fail_metadata_twice(redis, value, previous=None):
        nonlocal attempts
        if value.game.message_id is not None and attempts < 2:
            attempts += 1
            raise OSError("Synthetic metadata write outage")
        await original(redis, value, previous)

    monkeypatch.setattr(command.store, "save", fail_metadata_twice)
    message = make_message(rig.bot, from_user=WHITE, text="/chess_play")
    await command.ChessPlay.process(message, rig.redis, rig.supervisor)
    await quiet(rig)
    photos = [method for method in rig.session.methods if isinstance(method, SendPhoto)]
    assert len(photos) == 1
    active = await rig.store.active(rig.redis, rig.bot.id, message.chat.id)
    assert active is not None and active.game.message_id == 100
    controls = [method for method in rig.session.methods if isinstance(method, (EditMessageCaption, EditMessageMedia))]
    assert controls and controls[-1].reply_markup is not None
    assert PlayCallback.unpack(controls[-1].reply_markup.inline_keyboard[0][0].callback_data).action == "join"


async def test_simultaneous_start_retains_only_one_match_and_one_board(rig, monkeypatch):
    original = rig.store.active
    arrived = asyncio.Event()
    calls = 0

    async def racing_active(redis, bot_id, chat_id):
        nonlocal calls
        result = await original(redis, bot_id, chat_id)
        calls += 1
        if calls <= 2:
            if calls == 2:
                arrived.set()
            await arrived.wait()
        return result

    monkeypatch.setattr(command.store, "active", racing_active)
    first = make_message(rig.bot, from_user=WHITE, text="/chess_play")
    second = make_message(rig.bot, from_user=BLACK, text="/chess_play")
    await asyncio.wait_for(
        asyncio.gather(
            command.ChessPlay.process(first, rig.redis, rig.supervisor), command.ChessPlay.process(second, rig.redis, rig.supervisor)
        ),
        1,
    )
    await quiet(rig)
    assert len(command.ChessPlay.matches) == 1
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1
    assert len(rig.store.active_tokens) == 1


async def test_invitation_displays_creators_existing_global_rating(rig):
    rig.ratings.scores[WHITE.id] = 1120
    match = await create(rig)
    sent = next(method for method in rig.session.methods if isinstance(method, SendPhoto))
    assert "1120" in sent.caption
    assert match.saved.game.white_rating == 1120
