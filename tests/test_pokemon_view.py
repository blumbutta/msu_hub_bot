"""The answer stays hidden during voting and every bounded result is reachable."""

from dataclasses import replace

import pytest
from aiogram.types import MessageEntity
from aiogram.utils.formatting import Text

from msu_hub_bot.commands import pokemon_view
from msu_hub_bot.commands.pokemon_view import CAPTION_LIMIT, Player, render
from msu_hub_bot.commands.quiz_view import compact, user_label
from msu_hub_bot.providers.pokemon import Pokemon


POKEMON = Pokemon(
    id="25",
    name="Pikachu",
    image_url="https://images.example.test/25.png",
    source_url="https://pokemon.example.test/pokedex/25",
)


def entity_text(caption: str, entity: MessageEntity) -> str:
    raw = caption.encode("utf-16-le")
    return raw[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")


def test_active_caption_hides_pokemon_source_and_answers_even_in_entities():
    players = [Player(11, "Аня", "anya", POKEMON.name, True), Player(12, "Вася", None, "Bulbasaur")]
    view = render(POKEMON, players)
    assert "Кто этот покемон?" in view.caption
    assert "Ответили: 2" in view.caption
    assert "Аня (@anya)" in view.caption and "Вася" in view.caption
    assert "10 минут" in view.caption and "может любой" in view.caption
    assert "Верно: +1, ошибка: −1. Минимум за день — 0." in view.caption
    assert "Выбор каждого и цветную картинку покажу в конце" in view.caption
    for hidden in (POKEMON.name, POKEMON.source_url, POKEMON.image_url, "Bulbasaur", "✓", "✗"):
        assert hidden not in view.caption
    assert {entity.url for entity in view.entities if entity.url} == {"tg://user?id=11", "tg://user?id=12"}
    assert not any(entity.type == "spoiler" for entity in view.entities)
    assert (view.page, view.pages) == (0, 1)


@pytest.mark.parametrize("scored,expected", [(True, None), (False, "Не удалось подтвердить"), (None, "Записываю очки")])
def test_finished_caption_reveals_pokemon_and_each_answer(scored, expected):
    players = [Player(11, "Аня", "anya", POKEMON.name, True), Player(12, "Вася", None, "Bulbasaur")]
    view = render(POKEMON, players, closed=True, scored=scored)
    assert f"Это {POKEMON.name}!" in view.caption
    assert "Угадали 1 из 2" in view.caption
    assert f"✓ Аня (@anya) — {POKEMON.name}" in view.caption
    assert "✗ Вася — Bulbasaur" in view.caption
    assert "Верно: +1" not in view.caption
    if expected:
        assert expected in view.caption
    assert {entity.url for entity in view.entities if entity.url} == {"tg://user?id=11", "tg://user?id=12", POKEMON.source_url}


@pytest.mark.parametrize("closed", [False, True])
def test_six_ordinary_participants_fit_on_one_page(closed):
    players = [Player(index, f"Игрок {index}", f"player{index}", "Pikachu", True) for index in range(6)]
    view = render(POKEMON, players, closed=closed)
    assert (view.page, view.pages) == (0, 1)
    assert "Страница" not in view.caption
    assert len([entity for entity in view.entities if entity.url and entity.url.startswith("tg://")]) == 6


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("scored", [True, False, None])
@pytest.mark.parametrize("text", ["Я" * 1000, "🎨🧑‍🤝‍🧑" * 300, '<a href="https://example.test">\n& </a>' * 100])
def test_long_names_and_unicode_stay_bounded_on_every_page(closed, scored, text):
    pokemon = replace(POKEMON, name=text)
    players = [Player(index, text, text, text, index % 2 == 0) for index in range(1, 58)]
    pages = render(pokemon, players, closed=closed, scored=scored).pages
    seen = []
    for page in range(pages):
        view = render(pokemon, players, closed=closed, scored=scored, page=page)
        assert len(Text(view.caption)) <= CAPTION_LIMIT
        assert len(view.entities) <= 100
        assert view.caption == view.caption.rstrip()
        assert "…" in view.caption
        assert f"Страница {page + 1}/{pages}" in view.caption
        for entity in view.entities:
            assert entity.length > 0
            assert entity.offset + entity.length <= len(Text(view.caption))
            assert entity_text(view.caption, entity)
            if entity.url and entity.url.startswith("tg://user?id="):
                seen.append(int(entity.url.removeprefix("tg://user?id=")))
    assert seen == [player.user_id for player in players]


def test_short_names_cannot_exceed_telegram_entity_budget():
    players = [Player(index, "a", None) for index in range(1000)]
    pages = render(POKEMON, players).pages
    for page in range(pages):
        view = render(POKEMON, players, page=page)
        assert len(view.entities) <= 100
        assert len(Text(view.caption)) <= CAPTION_LIMIT


def test_only_selected_page_constructs_participant_entities(monkeypatch):
    players = [Player(index, "Игрок" * 30, "u" * 32, "Pikachu" * 30, True) for index in range(1000)]
    formatted = []

    def label(user_id, name, username):
        formatted.append(user_id)
        return user_label(user_id, name, username)

    monkeypatch.setattr(pokemon_view, "user_label", label)
    view = render(POKEMON, players, closed=True, page=50)
    visible = [int(entity.url.removeprefix("tg://user?id=")) for entity in view.entities if entity.url and entity.url.startswith("tg://")]
    assert formatted == visible
    assert 0 < len(formatted) < len(players)


@pytest.mark.parametrize("closed", [False, True])
def test_empty_and_out_of_bounds_pages_are_readable(closed):
    empty = render(POKEMON, [], closed=closed, page=100)
    assert (empty.page, empty.pages) == (0, 1)
    assert "никто не ответил" in empty.caption
    assert "Страница" not in empty.caption
    players = [Player(index, "Имя" * 30, "u" * 32, "Bulbasaur" * 30) for index in range(30)]
    first = render(POKEMON, players, closed=closed, page=-100)
    last = render(POKEMON, players, closed=closed, page=1000)
    assert first.page == 0
    assert last.page == last.pages - 1
    assert "tg://user?id=29" in [entity.url for entity in last.entities]
    assert "tg://user?id=0" not in [entity.url for entity in last.entities]


def test_untrusted_names_do_not_inject_markup():
    name = '<a href="https://untrusted.example.test">spoiler</a>\nnext'
    pokemon = replace(POKEMON, name=name)
    view = render(pokemon, [Player(1, "<i>Имя</i>\nТест", "@user", "<s>Ответ</s>")], closed=True)
    assert compact(name, 96) in view.caption
    assert "<i>Имя</i> Тест (@user)" in view.caption
    assert "<s>Ответ</s>" in view.caption
    assert {entity.type for entity in view.entities} == {"bold", "text_link"}
    assert {entity.url for entity in view.entities if entity.url} == {POKEMON.source_url, "tg://user?id=1"}
