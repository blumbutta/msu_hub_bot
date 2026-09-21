"""Cooperative choices survive restarts without acquiring an autonomous clock."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, SendPhoto
from aiogram.types import CallbackQuery

from msu_hub_bot.commands.quest_view import QuestCallback
from msu_hub_bot.games.quest import CHOICE_TIME, PUBLICATION_WINDOW, QuestService
from msu_hub_bot.providers.quest import QuestScene
from msu_hub_bot.storage.features import FeatureStore, FeatureWorker
from msu_hub_bot.telegram.wrapper import BotWrapper
from quiz_helpers import FeatureFixture, GameSession, PNG, settle
from telegram_helpers import make_message


class Story:
    id, digest, title, author = "synthetic", "abc", "Ночной поезд", "Тестовый автор"

    def start(self):
        return {"step": 0, "route": []}

    def view(self, state):
        step = state["step"]
        return QuestScene(
            f"Сцена {step}. " + ("Вы спаслись!" if step == 2 else "Вы стоите у двери."),
            () if step == 2 else ("Свет", "Дверь", "Ждать"),
            PNG,
        )

    def choose(self, state, index):
        return {"step": state["step"] + 1, "route": [*state["route"], index]}


class Provider:
    def __init__(self):
        self.book = Story()
        self.calls = []
        self.error = None

    async def load(self, story_id, expected_hash=None):
        self.calls.append((story_id, expected_hash))
        if self.error:
            raise self.error
        assert story_id == self.book.id
        assert expected_hash in {None, self.book.digest}
        return self.book


class Session(GameSession):
    def __init__(self, backend):
        super().__init__(backend)
        self.send_error = None
        self.send_hook = None
        self.text_error = None
        self.lost_message = None

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendMessage) and method.reply_markup is not None:
            if self.send_hook:
                await self.send_hook()
            sent = await super().make_request(bot, method, timeout)
            original = make_message(bot, message_id=method.reply_parameters.message_id)
            sent = sent.model_copy(update={"text": method.text, "reply_to_message": original})
            self.messages[sent.message_id] = sent
            if self.send_error:
                self.lost_message = sent
                raise self.send_error
            return sent
        if isinstance(method, EditMessageText):
            self.methods.append(method)
            if self.text_error and (not callable(self.text_error) or self.text_error(method)):
                raise TelegramBadRequest(method=method, message="message can't be edited")
            message = self.messages[method.message_id].model_copy(update={"text": method.text, "reply_markup": method.reply_markup})
            self.messages[method.message_id] = message
            return message
        return await super().make_request(bot, method, timeout)


def restart(rig):
    rig.store = FeatureStore(rig.backend)
    rig.worker = FeatureWorker(rig.store)
    rig.service = QuestService(rig.bot, rig.store, rig.worker, rig.provider, clock=lambda: rig.backend.now)


@pytest.fixture
async def rig():
    backend = FeatureFixture()
    session = Session(backend)
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    result = SimpleNamespace(
        backend=backend,
        session=session,
        bot=bot,
        provider=Provider(),
        message=make_message(bot, message_id=10, date=backend.now),
    )
    restart(result)
    yield result
    result.worker.stop()
    await session.close()


async def current(rig, source=None):
    source = source or rig.message
    return await rig.service.get(source.chat.id, rig.service.token(rig.bot.id, source.chat.id, source.message_id))


async def start(rig):
    await rig.service.start(rig.message, "synthetic")
    return await current(rig)


async def click(rig, row, action="vote", choice=0, user=42, message=None, step=None):
    data = QuestCallback(game_id=row.key, scene_version=row.value.step if step is None else step, action=action, value=str(choice))
    message = message or rig.session.messages[row.value.message_id]
    query = CallbackQuery.model_validate(
        {
            "id": uuid4().hex,
            "chat_instance": "synthetic",
            "message": message,
            "data": data.pack(),
            "from_user": {"id": user, "is_bot": False, "first_name": f"Игрок {user}", "username": f"user_{user}"},
        },
        context={"bot": rig.bot},
    )
    await rig.service.callback(query, data)
    return next(method for method in reversed(rig.session.methods) if isinstance(method, AnswerCallbackQuery)).text


async def test_no_clock_before_first_vote_and_every_new_scene_waits(rig):
    row = await start(rig)
    assert row.value.status == "active"
    assert row.value.deadline is None
    rig.backend.now += timedelta(days=3)
    restart(rig)
    await settle(rig)
    assert (await current(rig)).value.step == 0
    await click(rig, row, choice=1)
    row = await current(rig)
    assert row.value.deadline == rig.backend.now + CHOICE_TIME
    rig.backend.now = row.value.deadline
    await settle(rig)
    row = await current(rig)
    assert row.value.step == 1
    assert row.value.state["route"] == [1]
    assert row.value.deadline is None
    assert row.value.counts == [0, 0, 0]
    rig.backend.now += timedelta(days=2)
    await settle(rig)
    assert (await current(rig)).value.step == 1


async def test_changed_vote_does_not_restart_deadline_or_duplicate_voter(rig):
    row = await start(rig)
    await click(rig, row, choice=0)
    deadline = (await current(rig)).value.deadline
    rig.backend.now += timedelta(minutes=9)
    assert "Голос сохранён" in await click(rig, row, choice=2)
    await click(rig, row, choice=1, user=99)
    value = (await current(rig)).value
    assert value.deadline == deadline
    assert value.voters == 2
    assert value.counts == [0, 1, 1]
    votes = await rig.service.votes.list(row.scope, parent=f"{row.key}:0")
    assert len(votes) == 2
    assert all("(@user_" in item.value.label for item in votes)


async def test_any_participant_can_finish_but_empty_choice_stays_open(rig):
    row = await start(rig)
    assert await click(rig, row, action="finish", user=99) == "Сначала должен проголосовать хотя бы один участник."
    assert (await current(rig)).value.deadline is None
    await click(rig, row, choice=0)
    assert await click(rig, row, action="finish", user=99) == "Выбор завершён."
    await settle(rig)
    assert (await current(rig)).value.state["route"] == [0]


async def test_tie_chooses_only_leaders_once_and_survives_restart(rig):
    row = await start(rig)
    for user, choice in enumerate([0, 0, 1, 1, 2], 1):
        await click(rig, row, choice=choice, user=user)
    seen = []
    rig.service.choose = lambda options: seen.append(list(options)) or options[-1]
    await click(rig, row, action="finish")
    assert seen == [[0, 1]]
    assert (await current(rig)).value.selected == 1
    restart(rig)
    await settle(rig)
    assert (await current(rig)).value.state["route"] == [1]
    assert len(seen) == 1


async def test_stale_and_late_votes_cannot_affect_next_scene(rig):
    row = await start(rig)
    await click(rig, row, choice=2)
    rig.backend.now = (await current(rig)).value.deadline
    assert await click(rig, row, choice=0, user=99) == "Время выбора истекло."
    await settle(rig)
    assert "Сцена уже изменилась" in await click(rig, row, choice=0)
    result = (await current(rig)).value
    assert result.state["route"] == [2]
    assert result.voters == 0


async def test_final_releases_chat_even_if_telegram_edit_fails(rig):
    row = await start(rig)
    await click(rig, row)
    await click(rig, row, action="finish")
    await settle(rig)
    row = await current(rig)
    await click(rig, row)
    rig.session.text_error = lambda method: "Квест завершён" in method.text
    await click(rig, row, action="finish")
    await settle(rig)
    row = await current(rig)
    assert row.value.status == "finished"
    assert row.value.state["route"] == [0, 0]
    assert (await rig.service.chats.get(row.scope, "active")).value.active is None
    newer = rig.message.model_copy(update={"message_id": 11})
    await rig.service.start(newer, "synthetic")
    assert (await current(rig, newer)).value.status == "active"


async def test_one_active_quest_across_chat_topics_and_concurrent_starts(rig):
    other = rig.message.model_copy(update={"message_id": 11, "is_topic_message": True, "message_thread_id": 57})
    await asyncio.gather(rig.service.start(rig.message, "synthetic"), rig.service.start(other, "synthetic"))
    cards = [method for method in rig.session.methods if isinstance(method, SendMessage) and method.reply_markup]
    assert len(cards) == 1


async def test_concurrent_votes_and_finish_settle_at_most_one_step(rig):
    row = await start(rig)
    await asyncio.gather(*(click(rig, row, user=index, choice=index % 3) for index in range(1, 7)))
    row = await current(rig)
    assert row.value.voters == 6
    assert row.value.counts == [2, 2, 2]
    await asyncio.gather(*(click(rig, row, action="finish", user=index) for index in range(1, 7)))
    await settle(rig)
    assert (await current(rig)).value.step == 1


async def test_lost_commit_response_does_not_count_twice(rig):
    row = await start(rig)
    rig.backend.lose_after_commit = 1
    await click(rig, row, choice=1)
    assert (await current(rig)).value.counts == [0, 1, 0]
    rig.backend.lose_after_commit = 1
    await click(rig, row, action="finish")
    await settle(rig)
    assert (await current(rig)).value.state["route"] == [1]


async def test_uncertain_initial_send_never_repeats_and_recovers_callback(rig):
    rig.session.send_error = TimeoutError()
    await start(rig)
    row = await current(rig)
    assert row.value.status == "publishing"
    restart(rig)
    rig.session.send_error = None
    await click(rig, row, message=rig.session.lost_message)
    row = await current(rig)
    assert row.value.status == "active"
    assert row.value.counts == [1, 0, 0]
    assert len([method for method in rig.session.methods if isinstance(method, SendMessage) and method.reply_markup]) == 1


async def test_unreconciled_publication_releases_chat_after_restart(rig):
    rig.session.send_error = TimeoutError()
    await start(rig)
    rig.backend.now += PUBLICATION_WINDOW
    restart(rig)
    await settle(rig)
    row = await current(rig)
    assert row.value.status == "abandoned"
    assert (await rig.service.chats.get(row.scope, "active")).value.active is None
    assert len([method for method in rig.session.methods if isinstance(method, SendMessage) and method.reply_markup]) == 1


@pytest.mark.parametrize("change", ["message", "sender", "forward", "topic", "chat"])
async def test_forged_or_unrelated_cards_do_not_accept_votes(rig, change):
    row = await start(rig)
    message = rig.session.messages[row.value.message_id]
    if change == "message":
        message = message.model_copy(update={"message_id": 999})
    elif change == "sender":
        message = message.model_copy(update={"from_user": rig.message.from_user})
    elif change == "forward":
        message = make_message(
            rig.bot,
            **{**message.model_dump(mode="json"), "forward_origin": {"type": "hidden_user", "date": 1, "sender_user_name": "Someone"}},
        )
    elif change == "topic":
        message = message.model_copy(update={"message_thread_id": 78, "is_topic_message": True})
    else:
        message = message.model_copy(update={"chat": message.chat.model_copy(update={"id": -888})})
    assert await click(rig, row, message=message) == "Этот квест уже недоступен."
    assert (await current(rig)).value.voters == 0


async def test_ordinary_reply_thread_id_is_not_a_forum_topic(rig):
    row = await start(rig)
    message = rig.session.messages[row.value.message_id].model_copy(update={"message_thread_id": 10, "is_topic_message": False})
    assert "Голос сохранён" in await click(rig, row, message=message)


async def test_images_sent_once_then_edited_for_next_scene(rig):
    row = await start(rig)
    await settle(rig)
    assert (await current(rig)).value.photo_message_id is not None
    await click(rig, row)
    await click(rig, row, action="finish")
    await settle(rig)
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1
    assert rig.provider.calls[-1][1] == "abc"


async def test_uncertain_photo_is_not_sent_again_after_restart(rig):
    row = await start(rig)

    async def fail_photo(method):
        raise TimeoutError()

    rig.session.photo_hook = fail_photo
    await settle(rig)
    assert (await current(rig)).value.photo_attempted_step == 0
    rig.session.photo_hook = None
    restart(rig)
    await click(rig, row)
    await click(rig, row, action="finish")
    await settle(rig)
    assert (await current(rig)).value.photo_message_id is None
    assert not [method for method in rig.session.methods if isinstance(method, SendPhoto)]


async def test_provider_failure_retries_saved_winner_and_does_not_lock_chat_forever(rig):
    from msu_hub_bot.providers.quest import QuestError

    row = await start(rig)
    await settle(rig)
    await click(rig, row, choice=2)
    await click(rig, row, action="finish")
    rig.provider.error = QuestError("Unavailable")
    for attempt in range(3):
        await settle(rig)
        value = (await current(rig)).value
        assert value.advance_attempts == attempt + 1
        if attempt < 2:
            assert value.status == "advancing"
            assert value.selected == 2
            rig.backend.now += timedelta(seconds=15)
            restart(rig)
    assert value.status == "finished"
    assert "Квест остановлен" in value.text
    assert (await rig.service.chats.get(row.scope, "active")).value.active is None


async def test_provider_recovery_continues_same_winning_choice(rig):
    from msu_hub_bot.providers.quest import QuestError

    row = await start(rig)
    await settle(rig)
    await click(rig, row, choice=1)
    await click(rig, row, action="finish")
    rig.provider.error = QuestError("Unavailable")
    await settle(rig)
    rig.provider.error = None
    rig.backend.now += timedelta(seconds=15)
    restart(rig)
    await settle(rig)
    assert (await current(rig)).value.state["route"] == [1]
    assert (await current(rig)).value.deadline is None


async def test_finished_quest_cleanup_removes_votes_but_not_another_quest(rig):
    row = await start(rig)
    await settle(rig)
    for _ in range(2):
        await click(rig, row)
        await click(rig, row, action="finish")
        await settle(rig)
        row = await current(rig)
    other = rig.message.model_copy(update={"message_id": 11})
    await rig.service.start(other, "synthetic")
    rig.backend.now += timedelta(days=1)
    await settle(rig)
    assert await current(rig) is None
    assert not await rig.service.votes.list(row.scope)
    assert (await current(rig, other)).value.status == "active"


async def test_pagination_does_not_start_deadline_and_works_after_final(rig):
    row = await start(rig)
    assert await click(rig, row, action="page", choice=0) == ""
    assert (await current(rig)).value.deadline is None
    await settle(rig)
    for _ in range(2):
        await click(rig, row)
        await click(rig, row, action="finish")
        await settle(rig)
        row = await current(rig)
    assert await click(rig, row, action="page", choice=0) == ""
    assert (await current(rig)).value.status == "finished"


async def test_missing_controls_release_chat_instead_of_waiting_for_impossible_vote(rig):
    row = await start(rig)
    rig.session.text_error = True
    await settle(rig)
    value = (await current(rig)).value
    assert value.status == "abandoned"
    assert value.presentation_failed
    assert (await rig.service.chats.get(row.scope, "active")).value.active is None
    other = rig.message.model_copy(update={"message_id": 11})
    rig.session.text_error = None
    await rig.service.start(other, "synthetic")
    assert (await current(rig, other)).value.status == "active"


async def test_superseded_before_image_send_clears_marker_and_shows_next_image(rig):
    from msu_hub_bot.storage.features import RecordKey

    row = await start(rig)
    checks = 0

    async def current_lease():
        nonlocal checks
        checks += 1
        if checks == 1:
            return True
        current_row = await current(rig)
        value = current_row.value.model_copy(deep=True)
        assert value.photo_attempted_step == 0
        value.step = 1
        value.state = {"step": 1, "route": [0]}
        value.text = "Следующая сцена"
        tx = rig.service._tx(row.scope)
        tx.expect(current_row)
        tx.put(rig.service.games, row.key, value, status=value.status)
        rig.service._schedule(tx, row.key, "image", rig.backend.now)
        await rig.service._commit(tx)
        return False

    context = SimpleNamespace(job=SimpleNamespace(scope=row.scope, record=RecordKey("games", row.key)), current=current_lease)
    await rig.service._image(context)
    assert (await current(rig)).value.photo_attempted_step is None
    await settle(rig)
    assert (await current(rig)).value.photo_message_id is not None
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1


async def test_repeated_command_recovers_card_deleted_after_idle_scene_was_rendered(rig):
    row = await start(rig)
    await settle(rig)
    assert not await rig.worker.run_once()
    assert (await current(rig)).value.deadline is None
    rig.session.text_error = True
    restart(rig)
    retry = rig.message.model_copy(update={"message_id": 11})
    await rig.service.start(retry, "synthetic")
    assert (await current(rig)).value.status == "abandoned"
    assert (await rig.service.chats.get(row.scope, "active")).value.active is None
    assert "можно начать новый /quest" in rig.session.methods[-1].text
    assert await current(rig, retry) is None
    rig.session.text_error = None
    again = rig.message.model_copy(update={"message_id": 12})
    await rig.service.start(again, "synthetic")
    assert (await current(rig, again)).value.status == "active"


@pytest.mark.parametrize("error", ["timeout", "network", "not_modified", "bad_request"])
async def test_repeated_command_never_abandons_card_without_definitive_failure(rig, error):
    from aiogram.exceptions import TelegramNetworkError

    row = await start(rig)
    await settle(rig)
    row = await current(rig)
    previous = rig.session.make_request

    async def failing_request(bot, method, timeout=None):
        if isinstance(method, EditMessageText):
            if error == "timeout":
                raise TimeoutError()
            if error == "network":
                raise TelegramNetworkError(method=method, message="network error")
            text = "message is not modified" if error == "not_modified" else "unclassified bad request"
            raise TelegramBadRequest(method=method, message=text)
        return await previous(bot, method, timeout)

    rig.session.make_request = failing_request
    retry = rig.message.model_copy(update={"message_id": 11})
    await rig.service.start(retry, "synthetic")
    unchanged = await current(rig)
    assert unchanged.etag == row.etag
    assert unchanged.value == row.value
    assert unchanged.value.status == "active"
    assert unchanged.value.deadline is None
    assert unchanged.value.voters == 0
    assert (await rig.service.chats.get(row.scope, "active")).value.active == row.key
    assert await current(rig, retry) is None
