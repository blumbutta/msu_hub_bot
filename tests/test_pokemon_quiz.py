"""Pokémon silhouette rounds preserve votes and reveal the same creature durably."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageCaption, EditMessageMedia, SendPhoto
from aiogram.types import BufferedInputFile

from msu_hub_bot.games import definitions
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.pokemon import Pokemon, PokemonPuzzle
from msu_hub_bot.telegram.wrapper import BotWrapper
from quiz_helpers import FeatureFixture, GameSession, click, edits, restart, score_rows, score_values, settle, start, text_of
from telegram_helpers import make_message

POKEMON = Pokemon(
    id="25",
    name="Pikachu",
    image_url="https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/25.png",
    source_url="https://pokeapi.co/api/v2/pokemon-species/25/",
)
PUZZLE = PokemonPuzzle(
    pokemon=POKEMON,
    options=("Eevee", "Bulbasaur", POKEMON.name, "Squirtle", "Charmander", "Jigglypuff"),
    answer=2,
)
SOURCE_IMAGE = b"synthetic-transparent-source"
SILHOUETTE = b"synthetic-silhouette"
COLOUR_IMAGE = b"synthetic-colour-reveal"


@pytest.fixture
async def pokemon_rig(monkeypatch):
    monkeypatch.setattr("msu_hub_bot.games.quiz.EDIT_INTERVAL", 0)
    monkeypatch.setattr(definitions, "random_pokemon", AsyncMock(return_value=PUZZLE))
    monkeypatch.setattr(definitions, "download_pokemon", AsyncMock(return_value=SOURCE_IMAGE))
    monkeypatch.setattr(
        definitions,
        "render_pokemon",
        Mock(side_effect=lambda image, *, solution=False: COLOUR_IMAGE if solution else SILHOUETTE),
    )
    backend = FeatureFixture()
    session = GameSession(backend)
    bot = BotWrapper("123456789:" + "a" * 35, session=session)
    result = SimpleNamespace(
        feature="pokemon",
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
    return await rig.quiz.round("pokemon", record.value.chat_id, record.key)


async def votes(rig, record):
    return await rig.quiz.votes("pokemon", record.scope, record.key)


def short_publication_budget(monkeypatch):
    monkeypatch.setitem(definitions.DEFINITIONS, "pokemon", replace(definitions.DEFINITIONS["pokemon"], photo_timeout=0.02))


async def test_pokemon_publishes_silhouette_and_six_choices_without_answer_metadata(pokemon_rig):
    record = await start(pokemon_rig)
    (photo,) = [method for method in pokemon_rig.session.methods if isinstance(method, SendPhoto)]
    question = record.value.question
    assert record.value.phase == "active" and question.kind == "pokemon"
    assert question.identity == POKEMON.id and question.pokemon_name == POKEMON.name
    assert len(set(question.choices)) == 6 and question.choices[question.answer] == POKEMON.name
    assert isinstance(photo.photo, BufferedInputFile)
    assert photo.photo.data == SILHOUETTE and photo.photo.filename == "pokemon.png"
    definitions.download_pokemon.assert_awaited_once_with(POKEMON.image_url)
    definitions.render_pokemon.assert_called_once_with(SOURCE_IMAGE, solution=False)
    assert photo.reply_parameters.message_id == pokemon_rig.message.message_id and photo.message_thread_id == 17
    assert photo.parse_mode is None
    buttons = [button for row in photo.reply_markup.inline_keyboard for button in row]
    assert [button.text for button in buttons[:-1]] == list(PUZZLE.options)
    assert [button.callback_data for button in buttons[:-1]] == [f"pokemon:{record.key}:{index}" for index in range(6)]
    assert buttons[-1].text == "Завершить задание"
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons)
    assert not any(value in photo.caption for value in (*PUZZLE.options, POKEMON.image_url, POKEMON.source_url))
    assert not any(entity.url for entity in photo.caption_entities or [])
    assert definitions.DEFINITIONS["pokemon"].photo_timeout == 20
    assert record.value.deadline_at == record.value.published_at + timedelta(minutes=10)


async def test_pokemon_reserves_one_loading_slot_per_chat_across_topics(pokemon_rig):
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(_):
        entered.set()
        await release.wait()
        return PUZZLE

    definitions.random_pokemon.side_effect = blocked
    loading = asyncio.create_task(start(pokemon_rig))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await pokemon_rig.quiz.start("pokemon", pokemon_rig.message.model_copy(update={"message_id": 11, "message_thread_id": 99}))
        assert pokemon_rig.session.methods[-1].text == "Подождите, прошлое задание еще не окончено!"
        assert definitions.random_pokemon.await_count == 1
    finally:
        release.set()
    record = await loading
    other = await start(
        pokemon_rig,
        make_message(pokemon_rig.bot, chat={"id": -100999, "type": "supergroup"}, date=pokemon_rig.backend.now),
    )
    assert record.value.phase == other.value.phase == "active"


async def test_pokemon_votes_show_names_and_handles_but_keep_choices_and_colours_hidden(pokemon_rig):
    record = await start(pokemon_rig)
    answer = record.value.question.answer
    await click(pokemon_rig, record, answer, name="Аня <&>", username="anya")
    await click(pokemon_rig, record, (answer + 1) % 6, user_id=43, name="Борис", username="boris")
    await click(pokemon_rig, record, (answer + 1) % 6, name="Аня <&>", username="anya")
    await settle(pokemon_rig)
    stored = await votes(pokemon_rig, record)
    assert [(vote.value.user_id, vote.value.choice) for vote in stored] == [(42, answer), (43, (answer + 1) % 6)]
    edit = edits(pokemon_rig)[-1]
    caption = text_of(edit)
    assert "Ответили: 2" in caption and "Аня <&>" in caption and "@anya" in caption and "@boris" in caption
    assert not any(value in caption for value in (*PUZZLE.options, POKEMON.image_url, POKEMON.source_url))
    assert {entity.url for entity in edit.caption_entities if entity.url} == {"tg://user?id=42", "tg://user?id=43"}
    assert not any(isinstance(method, EditMessageMedia) for method in edits(pokemon_rig))
    definitions.render_pokemon.assert_called_once()
    acknowledgements = [method.text for method in pokemon_rig.session.methods if isinstance(method, AnswerCallbackQuery)]
    assert "Изменить его нельзя" in acknowledgements[-1]


async def test_pokemon_any_player_can_finish_and_reveal_colours_without_double_scoring(pokemon_rig):
    record = await start(pokemon_rig)
    answer = record.value.question.answer
    await click(pokemon_rig, record, answer, name="Аня", username="anya")
    await click(pokemon_rig, record, (answer + 1) % 6, user_id=43, name="Борис", username="boris")
    await asyncio.gather(click(pokemon_rig, record, "finish", user_id=999), click(pokemon_rig, record, "finish", user_id=888))
    await settle(pokemon_rig)
    closed = await current(pokemon_rig, record)
    assert closed.value.phase == "closed" and closed.value.score_status == "recorded"
    assert await score_values(pokemon_rig, closed.value.score_day) == {42: 1, 43: 0}
    media = [method for method in edits(pokemon_rig) if isinstance(method, EditMessageMedia)]
    assert media and media[-1].media.media.data == COLOUR_IMAGE
    assert media[-1].media.media.filename == "pokemon-solution.png" and media[-1].message_id == record.value.message_id
    definitions.render_pokemon.assert_any_call(SOURCE_IMAGE, solution=True)
    result = edits(pokemon_rig)[-1]
    assert all(value in text_of(result) for value in (POKEMON.name, PUZZLE.options[(answer + 1) % 6], "@anya", "@boris", "Угадали 1 из 2"))
    entities = result.media.caption_entities if isinstance(result, EditMessageMedia) else result.caption_entities
    assert POKEMON.source_url in {entity.url for entity in entities if entity.url}
    assert result.reply_markup is None
    assert len([method for method in pokemon_rig.session.methods if isinstance(method, SendPhoto)]) == 1
    restart(pokemon_rig)
    await click(pokemon_rig, record, "finish")
    await settle(pokemon_rig)
    assert await score_values(pokemon_rig, closed.value.score_day) == {42: 1, 43: 0}
    assert edits(pokemon_rig)[-1].media.media.data == COLOUR_IMAGE


async def test_pokemon_deadline_reveals_saved_creature_after_restart_on_original_score_day(pokemon_rig):
    pokemon_rig.backend.now = datetime(2030, 1, 1, 20, 45, tzinfo=UTC)
    record = await start(pokemon_rig)
    await click(pokemon_rig, record, record.value.question.answer)
    snapshot = (await current(pokemon_rig, record)).value.model_dump()
    restart(pokemon_rig)
    definitions.random_pokemon.side_effect = ExternalServiceError("must not choose a new creature")
    assert (await current(pokemon_rig, record)).value.model_dump() == snapshot
    pokemon_rig.backend.now = datetime(2030, 1, 1, 21, 1, tzinfo=UTC)
    await settle(pokemon_rig)
    closed = await current(pokemon_rig, record)
    assert closed.value.closed_at == record.value.deadline_at
    assert closed.value.score_day.isoformat() == "2030-01-01"
    assert closed.value.phase == "closed" and closed.value.score_status == "recorded"
    assert await score_values(pokemon_rig, closed.value.score_day) == {42: 1}
    media = [method for method in edits(pokemon_rig) if isinstance(method, EditMessageMedia)]
    assert media and media[-1].media.media.data == COLOUR_IMAGE
    assert definitions.download_pokemon.call_args.args == (POKEMON.image_url,)
    definitions.random_pokemon.assert_awaited_once()


async def test_pokemon_loading_does_not_consume_voting_time(pokemon_rig):
    async def delayed(_):
        pokemon_rig.backend.now += timedelta(seconds=18)
        return PUZZLE

    definitions.random_pokemon.side_effect = delayed
    record = await start(pokemon_rig)
    assert record.value.published_at == pokemon_rig.backend.now
    assert record.value.deadline_at - record.value.prepared_at == timedelta(minutes=10, seconds=18)


async def test_pokemon_scores_floor_at_zero_and_daily_ranking_is_independent(pokemon_rig):
    pokemon_rig.backend.now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    for index, correct in enumerate((False, True, False, False, True)):
        record = await start(pokemon_rig, pokemon_rig.message.model_copy(update={"message_id": 10 + index}))
        answer = record.value.question.answer
        await click(pokemon_rig, record, answer if correct else (answer + 1) % 6, name="Анна", username="anna")
        await click(pokemon_rig, record, "finish")
        await settle(pokemon_rig)
        day = (await current(pokemon_rig, record)).value.score_day
        assert await score_values(pokemon_rig, day) == {42: int(correct)}
    for feature in ("chess", "geoguess", "art"):
        assert await score_values(pokemon_rig, day, feature=feature) == {}
    assert await score_values(pokemon_rig, day, chat_id=-100999) == {}
    (player,) = await score_rows(pokemon_rig, day)
    assert player.value.name == "Анна" and player.value.username == "anna"
    text = (await pokemon_rig.quiz.ranking("pokemon", pokemon_rig.message.chat.id)).render()[0]
    assert "Анна" in text and "@anna" in text and "01.01.2030" in text
    pokemon_rig.backend.now = datetime(2030, 1, 1, 21, 1, tzinfo=UTC)
    text = (await pokemon_rig.quiz.ranking("pokemon", pokemon_rig.message.chat.id)).render()[0]
    assert "02.01.2030" in text and "Пока нет очков" in text and "@anna" not in text
    assert await score_values(pokemon_rig, day) == {42: 1}


async def test_pokemon_recent_history_preserves_last_fifteen_species_after_restart(pokemon_rig):
    identities = []
    for index in range(17):
        definitions.random_pokemon.return_value = replace(PUZZLE, pokemon=replace(POKEMON, id=str(1000 + index)))
        record = await start(pokemon_rig, pokemon_rig.message.model_copy(update={"message_id": 10 + index}))
        identities.append(record.value.question.identity)
        await click(pokemon_rig, record, "finish")
        await settle(pokemon_rig)
        restart(pokemon_rig)
    chat = await pokemon_rig.quiz.collections["pokemon"].chats.get(record.scope, "state")
    assert chat.value.recent == identities[-15:]
    assert definitions.random_pokemon.call_args.args[0] == tuple(identities[-16:-1])


@pytest.mark.parametrize("stage", ["provider", "download", "upload"])
async def test_pokemon_twenty_second_publication_budget_cancels_blocked_work_without_resending(pokemon_rig, monkeypatch, stage):
    short_publication_budget(monkeypatch)
    cancelled = asyncio.Event()

    async def blocked(_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    if stage == "provider":
        definitions.random_pokemon.side_effect = blocked
    elif stage == "download":
        definitions.download_pokemon.side_effect = blocked
    else:
        pokemon_rig.session.photo_hook = blocked
    record = await asyncio.wait_for(start(pokemon_rig), timeout=1)
    assert cancelled.is_set()
    assert record.value.phase == ("publishing" if stage == "upload" else "abandoned")
    assert not any(isinstance(method, SendPhoto) for method in pokemon_rig.session.methods)
    if stage != "upload":
        assert pokemon_rig.session.methods[-1].text == "Ошибка, попробуйте еще раз"
    restart(pokemon_rig)
    pokemon_rig.backend.now += timedelta(minutes=2)
    await settle(pokemon_rig)
    chat = await pokemon_rig.quiz.collections["pokemon"].chats.get(record.scope, "state")
    assert chat.value.active is None and chat.value.recent == []
    definitions.random_pokemon.assert_awaited_once()


@pytest.mark.parametrize("failure", ["download", "telegram"])
async def test_pokemon_temporary_reveal_failure_keeps_answer_and_repairs_same_photo_after_restart(pokemon_rig, failure):
    pokemon_rig.worker.concurrency = 1
    record = await start(pokemon_rig)
    await click(pokemon_rig, record, record.value.question.answer)
    if failure == "download":
        definitions.download_pokemon.side_effect = ExternalServiceError("temporary image failure")
    else:

        async def transient(method):
            if isinstance(method, EditMessageMedia):
                raise TimeoutError("lost media edit reply")

        pokemon_rig.session.edit_hook = transient
    await click(pokemon_rig, record, "finish")
    await settle(pokemon_rig)
    assert any(isinstance(method, EditMessageCaption) and POKEMON.name in method.caption for method in edits(pokemon_rig))
    closed = await current(pokemon_rig, record)
    assert closed.value.phase == "closed" and closed.value.score_status == "recorded"
    assert await score_values(pokemon_rig, closed.value.score_day) == {42: 1}
    restart(pokemon_rig)
    definitions.download_pokemon.side_effect = None
    definitions.random_pokemon.side_effect = ExternalServiceError("a reveal must not load a different question")
    pokemon_rig.session.edit_hook = None
    pokemon_rig.backend.now += timedelta(seconds=5)
    await settle(pokemon_rig)
    restored = edits(pokemon_rig)[-1]
    assert isinstance(restored, EditMessageMedia) and restored.media.media.data == COLOUR_IMAGE
    assert restored.message_id == record.value.message_id
    assert len([method for method in pokemon_rig.session.methods if isinstance(method, SendPhoto)]) == 1
    assert await score_values(pokemon_rig, closed.value.score_day) == {42: 1}
    definitions.random_pokemon.assert_awaited_once()


async def test_pokemon_failed_preparation_releases_slot_without_remembering_unpublished_species(pokemon_rig):
    definitions.download_pokemon.side_effect = ExternalServiceError("temporary image failure")
    record = await start(pokemon_rig)
    assert record.value.phase == "abandoned"
    assert pokemon_rig.session.methods[-1].text == "Ошибка, попробуйте еще раз"
    definitions.download_pokemon.side_effect = None
    newer = await start(pokemon_rig, pokemon_rig.message.model_copy(update={"message_id": 11}))
    assert newer.value.phase == "active"
    assert definitions.random_pokemon.call_args.args[0] == ()


async def test_pokemon_lost_send_confirmation_recovers_only_from_existing_card(pokemon_rig, monkeypatch):
    original = pokemon_rig.session.make_request

    async def lost(bot, method, timeout=None):
        result = await original(bot, method, timeout)
        if isinstance(method, SendPhoto):
            raise TimeoutError("photo exists but Telegram acknowledgement was lost")
        return result

    monkeypatch.setattr(pokemon_rig.session, "make_request", lost)
    record = await start(pokemon_rig)
    assert record.value.phase == "publishing" and record.value.message_id is None
    delivered = next(iter(pokemon_rig.session.messages.values()))
    restart(pokemon_rig)
    await click(pokemon_rig, record, "0", message=pokemon_rig.message)
    assert not await votes(pokemon_rig, record)
    await click(pokemon_rig, record, "0", message=delivered)
    restored = await current(pokemon_rig, record)
    assert restored.value.phase == "active" and restored.value.message_id == delivered.message_id
    assert restored.value.deadline_at == delivered.date + timedelta(minutes=10)
    assert len(await votes(pokemon_rig, record)) == 1
    await click(pokemon_rig, restored, "finish")
    await settle(pokemon_rig)
    assert any(isinstance(method, EditMessageMedia) and method.media.media.data == COLOUR_IMAGE for method in edits(pokemon_rig))
    assert len([method for method in pokemon_rig.session.methods if isinstance(method, SendPhoto)]) == 1
