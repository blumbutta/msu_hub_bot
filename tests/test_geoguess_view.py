from dataclasses import replace

import pytest
from aiogram.types import MessageEntity
from aiogram.utils.formatting import Text

from msu_hub_bot.commands import geoguess_view
from msu_hub_bot.commands.geoguess_view import CAPTION_LIMIT, PAGE_SIZE, Player, compact, country_label, render, user_label
from msu_hub_bot.providers.geoguess import Photo


PHOTO = Photo(
    country="Норвегия",
    city="Берген",
    url="https://upload.wikimedia.org/test.jpg",
    source="https://commons.wikimedia.org/?curid=1",
    author="Автор фотографии",
    license="CC BY-SA 4.0",
    license_url="https://creativecommons.org/licenses/by-sa/4.0/",
)


def entity_text(caption: str, entity: MessageEntity) -> str:
    raw = caption.encode("utf-16-le")
    return raw[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")


def test_active_page_hides_answers_and_location_but_keeps_photo_credit():
    players = [Player(11, "Первый", "first", "Норвегия", True), Player(12, "Второй", None, "Россия")]
    view = render(PHOTO, players, closed=False)
    assert "Ответили: 2" in view.caption
    assert "Первый (@first)" in view.caption and "Второй" in view.caption
    assert "10 минут" in view.caption and "может любой" in view.caption
    assert "Выбор каждого покажу в конце" in view.caption
    assert all(hidden not in view.caption for hidden in ("Норвегия", "Россия", "Берген", "✓", "✗"))
    assert PHOTO.author in view.caption and PHOTO.license in view.caption
    assert {entity.url for entity in view.entities if entity.url} == {
        "tg://user?id=11",
        "tg://user?id=12",
        PHOTO.license_url,
    }
    assert (view.page, view.pages) == (0, 1)


@pytest.mark.parametrize("scored,expected", [(True, "Верно: +1, ошибка: −1"), (False, "Не удалось подтвердить"), (None, "Записываю")])
def test_finished_page_reveals_each_answer_and_scoring_status(scored, expected):
    players = [Player(11, "Первый", "first", "Норвегия", True), Player(12, "Второй", None, "Россия")]
    view = render(PHOTO, players, closed=True, scored=scored)
    assert "Берген, 🇳🇴 Норвегия" in view.caption and "Угадали 1 из 2" in view.caption
    assert "✓ Первый (@first) — 🇳🇴 Норвегия" in view.caption
    assert "✗ Второй — 🇷🇺 Россия" in view.caption
    assert expected in view.caption
    assert {entity.url for entity in view.entities if entity.url} == {
        "tg://user?id=11",
        "tg://user?id=12",
        PHOTO.source,
        PHOTO.license_url,
        "https://www.openstreetmap.org/copyright",
    }
    assert PHOTO.author in view.caption and PHOTO.license in view.caption


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("scored", [None, False, True])
@pytest.mark.parametrize("text", ["Я" * 1000, "🌍🧑‍🤝‍🧑" * 300, '<a href="https://example.test">\n& </a>' * 100])
def test_long_metadata_and_unicode_fit_the_caption_and_have_valid_entities(closed, scored, text):
    photo = replace(PHOTO, country=text, city=text, author=text, license=text)
    players = [Player(index, text, text, text, index % 2 == 0) for index in range(1, 154)]
    for page in range((len(players) + PAGE_SIZE - 1) // PAGE_SIZE):
        view = render(photo, players, closed=closed, scored=scored, page=page)
        assert len(Text(view.caption)) <= CAPTION_LIMIT
        assert len(view.entities) <= 8
        for entity in view.entities:
            assert entity.length > 0
            assert entity.offset + entity.length <= len(Text(view.caption))
            assert entity_text(view.caption, entity)
        assert "…" in view.caption
        assert "Страница" in view.caption


@pytest.mark.parametrize("closed", [False, True])
def test_every_participant_is_reachable_exactly_once(closed):
    players = [Player(index, f"Игрок {index}", None, "Норвегия", True) for index in range(1, 158)]
    pages = render(PHOTO, players, closed=closed).pages
    profile_ids = []
    for page in range(pages):
        view = render(PHOTO, players, closed=closed, page=page)
        profile_ids.extend(
            int(entity.url.removeprefix("tg://user?id=")) for entity in view.entities if entity.url and entity.url.startswith("tg://")
        )
        assert view.page == page and view.pages == pages
        assert len(view.caption) <= CAPTION_LIMIT
    assert profile_ids == [player.user_id for player in players]


def test_only_the_selected_page_formats_participants(monkeypatch):
    players = [Player(index, str(index), None, "Норвегия", True) for index in range(1000)]
    formatted = []

    def label(user_id, name, username):
        formatted.append(user_id)
        return user_label(user_id, name, username)

    monkeypatch.setattr(geoguess_view, "user_label", label)
    render(PHOTO, players, closed=True, page=50)
    assert formatted == [200, 201, 202, 203]


@pytest.mark.parametrize("closed", [False, True])
def test_page_requests_are_clamped_and_empty_rounds_are_readable(closed):
    empty = render(PHOTO, [], closed=closed, page=100)
    assert (empty.page, empty.pages) == (0, 1)
    assert "никто не ответил" in empty.caption
    assert "Страница" not in empty.caption
    players = [Player(index, str(index), None, "Норвегия", True) for index in range(9)]
    first = render(PHOTO, players, closed=closed, page=-100)
    last = render(PHOTO, players, closed=closed, page=100)
    assert (first.page, first.pages) == (0, 3)
    assert (last.page, last.pages) == (2, 3)
    assert "tg://user?id=8" in [entity.url for entity in last.entities]
    assert "tg://user?id=7" not in [entity.url for entity in last.entities]


def test_user_content_is_literal_and_does_not_inject_entities_or_newlines():
    name = '<b>Игрок</b>\n<a href="https://example.test">'
    caption, entities = user_label(123, name, "first\nsecond").render()
    assert "<b>Игрок</b>" in caption
    assert "\n" not in caption
    assert "(@first second)" in caption
    assert len(entities) == 1 and entities[0].type == "text_link"
    assert entities[0].url == "tg://user?id=123"
    assert entity_text(caption, entities[0]) == compact(name, 48)


def test_long_names_keep_profile_links_and_usernames_are_bounded():
    text, entities = user_label(456, "🧑" * 100, "username" * 20).render()
    name = entity_text(text, entities[0])
    assert len(Text(name)) <= 48 and name.endswith("…")
    assert entities[0].url == "tg://user?id=456"
    assert len(Text(text)) <= 48 + 32 + 4
    assert user_label(456, "\n \t", None).render()[0] == "Игрок"


def test_compact_counts_utf16_without_cutting_a_surrogate_pair():
    assert compact(" A\n B\t C ", 5) == "A B C"
    assert compact("🌍🌍🌍", 5) == "🌍🌍…"
    assert compact("🌍🌍🌍", 4) == "🌍…"
    assert compact("🌍", 1) == "…"
    assert compact("🌍", 0) == ""
    assert country_label("Норвегия") == "🇳🇴 Норвегия"
    assert country_label("Другая страна") == "Другая страна"
