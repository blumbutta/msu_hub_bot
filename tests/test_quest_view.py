"""Story text and choices stay readable without starting a vote timer in the view."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from msu_hub_bot.commands.quest_view import QuestCallback, render
from msu_hub_bot.providers.quest import QuestScene


def view(scene=None, **changes):
    values = dict(
        title="Сигнал",
        token="aaaaaaaaaaaa",
        step=0,
        counts=[0, 0],
        voters=[],
        deadline=None,
        finished=False,
        last_choice=None,
    )
    values.update(changes)
    return render(scene or QuestScene("Вы оказались в вагоне.", ("Открыть дверь", "Остаться")), **values)


def test_waiting_scene_has_no_premature_deadline_and_an_early_finish_button():
    result = view()
    assert "Ждём первый голос" in result.text
    assert "10 минут" in result.text
    buttons = [button for row in result.keyboard.inline_keyboard for button in row]
    assert [button.text for button in buttons] == ["1. Открыть дверь", "2. Остаться", "⏭ Завершить выбор досрочно"]
    assert [QuestCallback.unpack(button.callback_data).action for button in buttons] == ["vote", "vote", "finish"]


def test_voting_shows_names_count_fixed_deadline_and_tie_rule():
    result = view(
        counts=[1, 1],
        voters=["Аня (@anya)", "Борис (@boris)"],
        deadline=datetime(2026, 9, 21, 12, 10, tzinfo=UTC),
    )
    assert "Голосов: 2" in result.text
    assert "Аня (@anya), Борис (@boris)" in result.text
    assert "15:10:00 МСК" in result.text
    assert "случайный вариант среди лидеров" in result.text
    assert "Выбор можно изменить" in result.text


def test_finish_explains_previous_choice_and_has_no_voting_controls():
    result = view(QuestScene("Все спаслись.", ()), finished=True, last_choice="Открыть дверь", author="Автор квеста")
    assert "Квест завершён" in result.text
    assert "Прошлый выбор: Открыть дверь" in result.text
    assert "Автор квеста" in result.text
    assert result.keyboard is None


def test_long_unicode_story_and_choices_are_paginated_without_lost_content():
    story = "🧭" * 3500
    long_choice = "Открыть " + "сложную дверь " * 350
    scene = QuestScene(story, (long_choice, "Ждать"))
    first = view(scene, voters=["🧑" * 60] * 100)
    assert first.pages > 1
    bodies = []
    for page in range(first.pages):
        result = view(scene, voters=["🧑" * 60] * 100, page=page)
        assert len(result.text.encode("utf-16-le")) // 2 <= 4096
        assert result.page == page
        assert "Выбрать вариант 1" == result.keyboard.inline_keyboard[0][0].text
        for row in result.keyboard.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64
        bodies.append(result.text.split("\n\n", 1)[1].rsplit("\n\n", 1)[0])
    complete = "".join(bodies)
    assert story in complete
    assert long_choice in complete


@pytest.mark.parametrize("page,expected", [(-10, 0), (999999, 2)])
def test_untrusted_page_numbers_are_clamped(page, expected):
    result = view(QuestScene("a" * 7000, ()), finished=True, page=page)
    assert result.page == expected


@pytest.mark.parametrize(
    "changes",
    [{"scene_version": -1}, {"action": "delete"}, {"value": "not-an-index"}, {"game_id": "x:y"}, {"game_id": "a" * 33}],
)
def test_callback_shape_is_bounded(changes):
    values = dict(game_id="abcdef123456", scene_version=0, action="vote", value="0")
    values.update(changes)
    with pytest.raises(ValidationError):
        QuestCallback(**values)
