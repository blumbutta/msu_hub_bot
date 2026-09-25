"""Artwork details stay secret while voting, and all results remain reachable."""

from dataclasses import replace

import pytest
from aiogram.types import MessageEntity
from aiogram.utils.formatting import Text

from msu_hub_bot.commands import art_view
from msu_hub_bot.commands.art_view import CAPTION_LIMIT, Player, render
from msu_hub_bot.commands.quiz_view import compact, user_label
from msu_hub_bot.providers.art import Artwork


ARTWORK = Artwork(
    id="1",
    title="Учебная картина",
    artist="Художник Первый",
    date="1900",
    image_url="https://images.example.test/art.jpg",
    source_url="https://museum.example.test/artworks/1",
)


def entity_text(caption: str, entity: MessageEntity) -> str:
    raw = caption.encode("utf-16-le")
    return raw[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")


def test_active_caption_has_no_answers_artwork_metadata_or_source_even_in_entities():
    players = [Player(11, "Аня", "anya", ARTWORK.artist, True), Player(12, "Вася", None, "Художник Второй")]
    view = render(ARTWORK, players)
    assert "Кто написал эту картину?" in view.caption
    assert "Ответили: 2" in view.caption
    assert "Аня (@anya)" in view.caption and "Вася" in view.caption
    assert "10 минут" in view.caption and "может любой" in view.caption
    assert "Верно: +1, ошибка: −1. Минимум за день — 0." in view.caption
    assert "Выбор каждого покажу в конце" in view.caption
    for hidden in (ARTWORK.title, ARTWORK.artist, ARTWORK.date, ARTWORK.source_url, ARTWORK.image_url, "Художник Второй", "✓", "✗"):
        assert hidden not in view.caption
    assert {entity.url for entity in view.entities if entity.url} == {"tg://user?id=11", "tg://user?id=12"}
    assert not any(entity.type == "spoiler" for entity in view.entities)
    assert (view.page, view.pages) == (0, 1)


@pytest.mark.parametrize("scored,expected", [(True, None), (False, "Не удалось подтвердить"), (None, "Записываю очки")])
def test_finished_caption_reveals_artwork_and_each_answer(scored, expected):
    players = [Player(11, "Аня", "anya", ARTWORK.artist, True), Player(12, "Вася", None, "Художник Второй")]
    view = render(ARTWORK, players, closed=True, scored=scored)
    assert f"Автор: {ARTWORK.artist}" in view.caption
    assert f"«{ARTWORK.title}», {ARTWORK.date}" in view.caption
    assert "Угадали 1 из 2" in view.caption
    assert f"✓ Аня (@anya) — {ARTWORK.artist}" in view.caption
    assert "✗ Вася — Художник Второй" in view.caption
    assert "Верно: +1" not in view.caption
    if expected:
        assert expected in view.caption
    assert {entity.url for entity in view.entities if entity.url} == {"tg://user?id=11", "tg://user?id=12", ARTWORK.source_url}
    assert not any(entity.type == "spoiler" for entity in view.entities)


@pytest.mark.parametrize("closed", [False, True])
def test_six_ordinary_participants_fit_without_unnecessary_second_page(closed):
    players = [Player(index, f"Игрок {index}", f"player{index}", "Художник", True) for index in range(6)]
    view = render(ARTWORK, players, closed=closed)
    assert (view.page, view.pages) == (0, 1)
    assert "Страница" not in view.caption
    assert len([entity for entity in view.entities if entity.url and entity.url.startswith("tg://")]) == 6


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("scored", [True, False, None])
@pytest.mark.parametrize("text", ["Я" * 1000, "🎨🧑‍🤝‍🧑" * 300, '<a href="https://example.test">\n& </a>' * 100])
def test_long_metadata_and_unicode_stay_bounded_on_every_page(closed, scored, text):
    artwork = replace(ARTWORK, title=text, artist=text, date=text)
    players = [Player(index, text, text, text, index % 2 == 0) for index in range(1, 58)]
    pages = render(artwork, players, closed=closed, scored=scored).pages
    seen = []
    for page in range(pages):
        view = render(artwork, players, closed=closed, scored=scored, page=page)
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
    pages = render(ARTWORK, players).pages
    for page in range(pages):
        view = render(ARTWORK, players, page=page)
        assert len(view.entities) <= 100
        assert len(Text(view.caption)) <= CAPTION_LIMIT


def test_only_selected_page_constructs_participant_entities(monkeypatch):
    players = [Player(index, "Игрок" * 30, "u" * 32, "Художник" * 30, True) for index in range(1000)]
    formatted = []

    def label(user_id, name, username):
        formatted.append(user_id)
        return user_label(user_id, name, username)

    monkeypatch.setattr(art_view, "user_label", label)
    view = render(ARTWORK, players, closed=True, page=50)
    visible = [int(entity.url.removeprefix("tg://user?id=")) for entity in view.entities if entity.url and entity.url.startswith("tg://")]
    assert formatted == visible
    assert 0 < len(formatted) < len(players)


@pytest.mark.parametrize("closed", [False, True])
def test_empty_and_out_of_bounds_pages_are_readable(closed):
    empty = render(ARTWORK, [], closed=closed, page=100)
    assert (empty.page, empty.pages) == (0, 1)
    assert "никто не ответил" in empty.caption
    assert "Страница" not in empty.caption
    players = [Player(index, "Имя" * 30, "u" * 32, "Художник" * 30) for index in range(30)]
    first = render(ARTWORK, players, closed=closed, page=-100)
    last = render(ARTWORK, players, closed=closed, page=1000)
    assert first.page == 0
    assert last.page == last.pages - 1
    assert "tg://user?id=29" in [entity.url for entity in last.entities]
    assert "tg://user?id=0" not in [entity.url for entity in last.entities]


def test_missing_date_does_not_leave_dangling_punctuation():
    view = render(replace(ARTWORK, date=""), [], closed=True)
    assert f"«{ARTWORK.title}»." in view.caption


def test_untrusted_metadata_and_player_names_do_not_inject_markup():
    title = '<a href="https://untrusted.example.test">spoiler</a>\nnext'
    artwork = replace(ARTWORK, title=title, artist="<b>Художник</b>")
    view = render(artwork, [Player(1, "<i>Имя</i>\nТест", "@user", "<s>Ответ</s>")], closed=True)
    assert compact(title, 140) in view.caption
    assert "<i>Имя</i> Тест (@user)" in view.caption
    assert "<s>Ответ</s>" in view.caption
    assert {entity.type for entity in view.entities} == {"bold", "text_link"}
    assert {entity.url for entity in view.entities if entity.url} == {ARTWORK.source_url, "tg://user?id=1"}
