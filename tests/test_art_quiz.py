"""Art quiz contracts at the durable storage and Telegram boundaries."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import AnswerCallbackQuery, SendPhoto
from aiogram.types import BufferedInputFile

from msu_hub_bot.games import definitions
from msu_hub_bot.games.quiz import PHOTO_TIMEOUT, ROUND_TIMEOUT
from msu_hub_bot.providers.art import ArtPuzzle, Artwork
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.telegram.wrapper import BotWrapper
from quiz_helpers import FeatureFixture, GameSession, click, edits, restart, score_rows, score_values, settle, start, text_of
from telegram_helpers import make_message

ARTWORK = Artwork(
    id="123",
    title="Synthetic river <at dusk>",
    artist="Artist <three>",
    date="1901",
    image_url="https://openaccess-cdn.clevelandart.org/1901.1/1901.1_web.jpg",
    source_url="https://www.clevelandart.org/art/1901.1",
)
ART_IMAGE = b"synthetic-art-image"
ART_PUZZLE = ArtPuzzle(
    artwork=ARTWORK,
    options=("Artist one", "Artist two", ARTWORK.artist, "Artist four", "Artist five", "Artist six"),
    answer=2,
)


@pytest.fixture
async def art_rig(monkeypatch):
    monkeypatch.setattr("msu_hub_bot.games.quiz.EDIT_INTERVAL", 0)
    monkeypatch.setattr(definitions, "random_artwork", AsyncMock(return_value=ART_PUZZLE))
    monkeypatch.setattr(definitions, "download_artwork", AsyncMock(return_value=ART_IMAGE))
    backend = FeatureFixture()
    session = GameSession(backend)
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    result = SimpleNamespace(
        feature="art",
        backend=backend,
        session=session,
        bot=bot,
        message=make_message(bot, message_id=10, date=backend.now, is_topic_message=True, message_thread_id=17),
    )
    restart(result)
    yield result
    result.worker.stop()
    await session.close()


async def current(rig, record):
    return await rig.quiz.round("art", record.value.chat_id, record.key)


async def votes(rig, record):
    return await rig.quiz.votes("art", record.scope, record.key)


def short_publication_budget(monkeypatch):
    monkeypatch.setitem(definitions.DEFINITIONS, "art", replace(definitions.DEFINITIONS["art"], photo_timeout=0.02))


async def test_art_photo_has_six_artist_buttons_without_answer_metadata(art_rig):
    record = await start(art_rig)
    (photo,) = [method for method in art_rig.session.methods if isinstance(method, SendPhoto)]
    question = record.value.question
    assert record.value.phase == "active" and question.kind == "art"
    assert len(set(question.choices)) == 6 and question.choices[question.answer] == ARTWORK.artist
    assert isinstance(photo.photo, BufferedInputFile) and photo.photo.data == ART_IMAGE and photo.photo.filename == "art.jpg"
    definitions.download_artwork.assert_awaited_once_with(ARTWORK.image_url)
    assert photo.reply_parameters.message_id == art_rig.message.message_id and photo.message_thread_id == 17
    assert photo.parse_mode is None
    buttons = [button for row in photo.reply_markup.inline_keyboard for button in row]
    assert [button.text for button in buttons[:-1]] == list(ART_PUZZLE.options)
    assert [button.callback_data for button in buttons[:-1]] == [f"art:{record.key}:{index}" for index in range(6)]
    assert buttons[-1].text == "Завершить задание"
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons)
    assert not any(value in photo.caption for value in (ARTWORK.artist, ARTWORK.title, ARTWORK.date, ARTWORK.source_url))
    assert not any(entity.url for entity in photo.caption_entities or [])
    assert definitions.DEFINITIONS["art"].photo_timeout == 20
    assert PHOTO_TIMEOUT == 10 and ROUND_TIMEOUT == timedelta(minutes=10)
    assert all(definitions.DEFINITIONS[feature].photo_timeout is None for feature in ("chess", "geoguess"))


async def test_art_loading_reserves_one_slot_across_topics_but_not_chats(art_rig):
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(_):
        entered.set()
        await release.wait()
        return ART_PUZZLE

    definitions.random_artwork.side_effect = blocked
    loading = asyncio.create_task(start(art_rig))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await art_rig.quiz.start("art", art_rig.message.model_copy(update={"message_id": 11, "message_thread_id": 99}))
        assert art_rig.session.methods[-1].text == "Подождите, прошлое задание еще не окончено!"
        assert definitions.random_artwork.await_count == 1
    finally:
        release.set()
    record = await loading
    other = await start(
        art_rig,
        make_message(art_rig.bot, chat={"id": -100999, "type": "supergroup"}, date=art_rig.backend.now),
    )
    assert record.value.phase == other.value.phase == "active"


async def test_art_votes_show_names_and_handles_but_hide_choices(art_rig):
    record = await start(art_rig)
    answer = record.value.question.answer
    await click(art_rig, record, answer, name="Аня <&>", username="anya")
    await click(art_rig, record, (answer + 1) % 6, user_id=43, name="Борис", username="boris")
    await click(art_rig, record, (answer + 1) % 6, name="Аня <&>", username="anya")
    await settle(art_rig)
    stored = await votes(art_rig, record)
    assert [(vote.value.user_id, vote.value.choice) for vote in stored] == [(42, answer), (43, (answer + 1) % 6)]
    edit = edits(art_rig)[-1]
    caption = text_of(edit)
    assert "Ответили: 2" in caption and "Аня <&>" in caption and "@anya" in caption and "@boris" in caption
    assert not any(value in caption for value in (*ART_PUZZLE.options, ARTWORK.title, ARTWORK.source_url, ARTWORK.date))
    assert {entity.url for entity in edit.caption_entities if entity.url} == {"tg://user?id=42", "tg://user?id=43"}
    acknowledgements = [method.text for method in art_rig.session.methods if isinstance(method, AnswerCallbackQuery)]
    assert "Изменить его нельзя" in acknowledgements[-1]


async def test_art_any_player_can_finish_and_reveal_painting_and_all_votes(art_rig):
    record = await start(art_rig)
    answer = record.value.question.answer
    await click(art_rig, record, answer, name="Аня", username="anya")
    await click(art_rig, record, (answer + 1) % 6, user_id=43, name="Борис", username="boris")
    await asyncio.gather(click(art_rig, record, "finish", user_id=999), click(art_rig, record, "finish", user_id=888))
    await settle(art_rig)
    closed = await current(art_rig, record)
    assert closed.value.phase == "closed" and closed.value.score_status == "recorded"
    assert await score_values(art_rig, closed.value.score_day) == {42: 1, 43: 0}
    edit = edits(art_rig)[-1]
    result = text_of(edit)
    assert all(value in result for value in (ARTWORK.title, ARTWORK.artist, ARTWORK.date, "@anya", "@boris", "Угадали 1 из 2"))
    assert ARTWORK.source_url in {entity.url for entity in edit.caption_entities if entity.url}
    assert edit.reply_markup is None
    assert len([method for method in art_rig.session.methods if isinstance(method, SendPhoto)]) == 1
    restart(art_rig)
    await click(art_rig, record, "finish")
    await settle(art_rig)
    assert await score_values(art_rig, closed.value.score_day) == {42: 1, 43: 0}


async def test_art_deadline_survives_restart_without_refetching_painting(art_rig):
    art_rig.backend.now = datetime(2030, 1, 1, 20, 45, tzinfo=UTC)
    record = await start(art_rig)
    await click(art_rig, record, record.value.question.answer)
    snapshot = (await current(art_rig, record)).value.model_dump()
    assert record.value.deadline_at == record.value.published_at + timedelta(minutes=10)
    restart(art_rig)
    definitions.random_artwork.side_effect = ExternalServiceError("must not fetch an existing question")
    assert (await current(art_rig, record)).value.model_dump() == snapshot
    art_rig.backend.now = datetime(2030, 1, 1, 21, 1, tzinfo=UTC)
    await settle(art_rig)
    closed = await current(art_rig, record)
    assert closed.value.closed_at == record.value.deadline_at
    assert closed.value.score_day.isoformat() == "2030-01-01"
    assert closed.value.phase == "closed" and closed.value.score_status == "recorded"
    assert await score_values(art_rig, closed.value.score_day) == {42: 1}
    definitions.random_artwork.assert_awaited_once()


async def test_art_slow_loading_does_not_consume_voting_time(art_rig):
    async def delayed(_):
        art_rig.backend.now += timedelta(seconds=18)
        return ART_PUZZLE

    definitions.random_artwork.side_effect = delayed
    record = await start(art_rig)
    assert record.value.published_at == art_rig.backend.now
    assert record.value.deadline_at - record.value.prepared_at == timedelta(minutes=10, seconds=18)


async def test_art_scores_floor_at_zero_and_are_separate_by_day_game_and_chat(art_rig):
    art_rig.backend.now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    for index, correct in enumerate((False, True, False, False, True)):
        record = await start(art_rig, art_rig.message.model_copy(update={"message_id": 10 + index}))
        answer = record.value.question.answer
        await click(art_rig, record, answer if correct else (answer + 1) % 6, name="Анна", username="anna")
        await click(art_rig, record, "finish")
        await settle(art_rig)
        day = (await current(art_rig, record)).value.score_day
        assert await score_values(art_rig, day) == {42: int(correct)}
    for feature in ("chess", "geoguess"):
        assert await score_values(art_rig, day, feature=feature) == {}
    assert await score_values(art_rig, day, chat_id=-100999) == {}
    (player,) = await score_rows(art_rig, day)
    assert player.value.name == "Анна" and player.value.username == "anna"
    text = (await art_rig.quiz.ranking("art", art_rig.message.chat.id)).render()[0]
    assert "Анна" in text and "@anna" in text and "01.01.2030" in text
    art_rig.backend.now = datetime(2030, 1, 1, 21, 1, tzinfo=UTC)
    text = (await art_rig.quiz.ranking("art", art_rig.message.chat.id)).render()[0]
    assert "02.01.2030" in text and "Пока нет очков" in text and "@anna" not in text
    assert await score_values(art_rig, day) == {42: 1}


async def test_art_recent_history_passes_last_fifteen_delivered_paintings_after_restart(art_rig):
    identities = []
    for index in range(17):
        definitions.random_artwork.return_value = replace(ART_PUZZLE, artwork=replace(ARTWORK, id=str(1000 + index)))
        record = await start(art_rig, art_rig.message.model_copy(update={"message_id": 10 + index}))
        identities.append(record.value.question.identity)
        await click(art_rig, record, "finish")
        await settle(art_rig)
        restart(art_rig)
    chat = await art_rig.quiz.collections["art"].chats.get(record.scope, "state")
    assert chat.value.recent == identities[-15:]
    assert definitions.random_artwork.call_args.args[0] == tuple(identities[-16:-1])


@pytest.mark.parametrize("stage", ["provider", "download", "upload"])
async def test_art_publication_budget_cancels_blocked_work_without_blind_retry(art_rig, monkeypatch, stage):
    short_publication_budget(monkeypatch)
    cancelled = asyncio.Event()

    async def blocked(_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    if stage == "provider":
        definitions.random_artwork.side_effect = blocked
    elif stage == "download":
        definitions.download_artwork.side_effect = blocked
    else:
        art_rig.session.photo_hook = blocked
    record = await asyncio.wait_for(start(art_rig), timeout=1)
    assert cancelled.is_set()
    assert record.value.phase == ("publishing" if stage == "upload" else "abandoned")
    assert not any(isinstance(method, SendPhoto) for method in art_rig.session.methods)
    if stage != "upload":
        assert art_rig.session.methods[-1].text == "Ошибка, попробуйте еще раз"
    restart(art_rig)
    art_rig.backend.now += timedelta(minutes=2)
    await settle(art_rig)
    chat = await art_rig.quiz.collections["art"].chats.get(record.scope, "state")
    assert chat.value.active is None and chat.value.recent == []
    definitions.random_artwork.assert_awaited_once()


async def test_art_budget_also_bounds_storage_before_provider(art_rig, monkeypatch):
    short_publication_budget(monkeypatch)
    original = art_rig.backend.feature_request
    cancelled = asyncio.Event()

    async def blocked(operation, request):
        if operation == "get":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return await original(operation, request)

    monkeypatch.setattr(art_rig.backend, "feature_request", blocked)
    await asyncio.wait_for(art_rig.quiz.start("art", art_rig.message), timeout=1)
    assert cancelled.is_set()
    definitions.random_artwork.assert_not_awaited()
    assert not any(isinstance(method, SendPhoto) for method in art_rig.session.methods)
    assert not art_rig.quiz._locks


async def test_art_cancelled_provider_releases_slot_and_does_not_remember_unsent_art(art_rig):
    entered = asyncio.Event()

    async def blocked(_):
        entered.set()
        await asyncio.Event().wait()

    definitions.random_artwork.side_effect = blocked
    task = asyncio.create_task(start(art_rig))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await settle(art_rig)
    chat = await art_rig.quiz.collections["art"].chats.get(art_rig.quiz._scope(art_rig.message.chat.id), "state")
    assert chat.value.active is None and chat.value.recent == []
    assert not any(isinstance(method, SendPhoto) for method in art_rig.session.methods)


async def test_art_uncertain_photo_is_recovered_only_by_existing_telegram_card(art_rig, monkeypatch):
    original = art_rig.session.make_request

    async def lost(bot, method, timeout=None):
        result = await original(bot, method, timeout)
        if isinstance(method, SendPhoto):
            raise TimeoutError("photo exists but Telegram acknowledgement was lost")
        return result

    monkeypatch.setattr(art_rig.session, "make_request", lost)
    record = await start(art_rig)
    assert record.value.phase == "publishing" and record.value.message_id is None
    delivered = next(iter(art_rig.session.messages.values()))
    restart(art_rig)
    await click(art_rig, record, "0", message=art_rig.message)
    assert not await votes(art_rig, record)
    await click(art_rig, record, "0", message=delivered)
    restored = await current(art_rig, record)
    assert restored.value.phase == "active" and restored.value.message_id == delivered.message_id
    assert restored.value.deadline_at == delivered.date + timedelta(minutes=10)
    assert len(await votes(art_rig, record)) == 1
    assert len([method for method in art_rig.session.methods if isinstance(method, SendPhoto)]) == 1


async def test_art_provider_failure_frees_chat_and_does_not_add_recent_entry(art_rig):
    definitions.random_artwork.side_effect = ExternalServiceError("synthetic provider failure")
    record = await start(art_rig)
    assert record.value.phase == "abandoned"
    assert art_rig.session.methods[-1].text == "Ошибка, попробуйте еще раз"
    definitions.random_artwork.side_effect = None
    newer = await start(art_rig, art_rig.message.model_copy(update={"message_id": 11}))
    assert newer.value.phase == "active"
    assert definitions.random_artwork.call_args.args[0] == ()


async def test_art_unavailable_storage_does_not_accept_unsaved_votes(art_rig):
    record = await start(art_rig)
    art_rig.backend.fail = True
    await click(art_rig, record, "0")
    assert "Не удалось подтвердить" in art_rig.session.methods[-1].text
    art_rig.backend.fail = False
    assert not await votes(art_rig, record)
