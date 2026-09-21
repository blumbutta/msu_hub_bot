"""The offline walkthrough follows real branches and includes native artwork."""

from io import BytesIO

import pytest
from PIL import Image

from msu_hub_bot.providers.quest import QuestError
from msu_hub_bot.providers.quest_demo import demo_book


@pytest.mark.parametrize("item", range(3))
@pytest.mark.parametrize("route", range(2))
@pytest.mark.parametrize("action", range(2))
def test_demo_branches_end_and_have_images(item, route, action):
    book = demo_book()
    state = book.start()
    original = dict(state)
    assert len(book.view(state).choices) == 3
    state = book.choose(state, item)
    assert original == book.start()
    for choice in (route, action):
        scene = book.view(state)
        with Image.open(BytesIO(scene.image)) as picture:
            assert picture.size == (900, 480)
        state = book.choose(state, choice)
    ending = book.view(state)
    assert not ending.choices
    assert "Все спаслись" in ending.text if route == action == 0 else "До рассвета" in ending.text
    with pytest.raises(QuestError):
        book.choose(state, 0)


def test_first_decision_changes_last_available_action():
    book = demo_book()
    paths = [book.choose(book.choose(book.start(), first), 0) for first in range(3)]
    assert len({book.view(state).choices[0] for state in paths}) == 3


@pytest.mark.parametrize("state", [{}, {"node": "tower"}, {"node": "shore", "item": "fuel"}, {"node": "elsewhere"}])
def test_invalid_demo_state_is_not_silently_reset(state):
    if not state:
        # A default state is also a valid starting snapshot.
        assert len(demo_book().view(state).choices) == 3
    else:
        with pytest.raises(QuestError):
            demo_book().view(state)
