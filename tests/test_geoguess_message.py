"""One-message game ownership, paging and concurrent caption updates."""

import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageCaption, SendMessage, SendPhoto

from msu_hub_bot.commands import geoguess as game
from test_geoguess_v3 import click, rig as rig, start


def seed(round_, count=21):
    round_.votes = {i: (i % 6, f"Player {i:04d} 🧪 <&>") for i in range(count)}
    round_.usernames = {i: None for i in range(count)}


def edits(rig):
    return [method for method in rig.session.methods if isinstance(method, EditMessageCaption)]


async def test_every_result_is_paged_on_the_original_photo(rig):
    round_ = await start(rig)
    seed(round_)
    await game.Geoguess.update_board(round_)
    await click(rig, round_, "finish")
    seen = set()
    for page in range(round_.view.pages):
        await click(rig, round_, f"page_{page}")
        view = round_.view
        assert len(view.caption.encode("utf-16-le")) // 2 <= 1024
        assert len(view.entities) < 100
        seen.update(entity.url for entity in view.entities if entity.url and entity.url.startswith("tg://user"))
    assert seen == {f"tg://user?id={i}" for i in range(21)}
    assert len([method for method in rig.session.methods if isinstance(method, SendPhoto)]) == 1
    assert not any(isinstance(method, SendMessage) for method in rig.session.methods)
    assert {method.message_id for method in edits(rig)} == {round_.message.message_id}
    assert all(method.parse_mode is None for method in edits(rig))
    assert all(
        len(button.callback_data.encode()) <= 64
        for method in edits(rig)
        if method.reply_markup
        for row in method.reply_markup.inline_keyboard
        for button in row
    )
    rig.client.eval.assert_awaited_once()


async def test_finished_pages_work_while_another_round_is_active(rig):
    previous = await start(rig)
    seed(previous)
    await click(rig, previous, "finish")
    current = await start(rig)
    await click(rig, previous, "page_2")
    assert previous.page == 2 and edits(rig)[-1].message_id == previous.message.message_id
    await click(rig, previous, 0, user_id=77)
    assert 77 not in previous.votes and not current.votes
    assert game.Geoguess.rounds[rig.message.chat.id] is current
    rig.client.eval.assert_awaited_once()
    game.Geoguess.completed.clear()
    before = len(edits(rig))
    await click(rig, previous, "page_1")
    assert "недоступен" in rig.session.methods[-1].text
    assert len(edits(rig)) == before


@pytest.mark.parametrize("page", ["page_-1", "page_", "page_1.5", "page_١", "page_999999999"])
async def test_invalid_page_does_not_vote_or_edit(rig, page):
    round_ = await start(rig)
    await click(rig, round_, page)
    assert not round_.votes and not edits(rig)
    rig.client.eval.assert_not_awaited()


async def test_valid_out_of_range_page_clamps_and_identical_edits_are_skipped(rig):
    round_ = await start(rig)
    seed(round_)
    await click(rig, round_, "page_99999999")
    assert round_.page == round_.view.pages - 1
    before = len(edits(rig))
    await game.Geoguess.update_board(round_)
    await click(rig, round_, "page_99999999")
    assert len(edits(rig)) == before


async def test_deleted_photo_never_generates_replacement_game_messages(rig):
    round_ = await start(rig)
    seed(round_)
    rig.session.caption_error = True
    await click(rig, round_, "finish")
    assert not game.Geoguess.rounds
    assert round_.view.pages == 1  # Only the initial caption was acknowledged.
    assert not any(isinstance(method, SendMessage) for method in rig.session.methods)
    rig.session.caption_error = False
    await click(rig, round_, "page_1")
    assert "На снимке" in round_.view.caption and round_.view.page == 1
    rig.client.eval.assert_awaited_once()


async def test_failed_small_round_reveal_retries_from_the_delivered_finish_button(rig):
    round_ = await start(rig)
    await click(rig, round_, 0)
    rig.session.caption_error = True
    await click(rig, round_, "finish")
    assert "Угадай страну" in round_.view.caption
    assert round_.view.pages == 1
    rig.session.caption_error = False
    await click(rig, round_, "finish")
    assert "На снимке" in round_.view.caption
    assert edits(rig)[-1].reply_markup is None
    rig.client.eval.assert_awaited_once()


async def test_not_modified_is_acknowledged_and_not_retried(rig, monkeypatch):
    round_ = await start(rig)
    seed(round_)
    request = rig.session.make_request
    attempted = []

    async def not_modified(bot, method, timeout=None):
        if isinstance(method, EditMessageCaption):
            attempted.append(method)
            raise TelegramBadRequest(method=method, message="Bad Request: message is not modified: content and markup are identical")
        return await request(bot, method, timeout)

    monkeypatch.setattr(rig.session, "make_request", not_modified)
    await game.Geoguess.update_board(round_)
    await game.Geoguess.update_board(round_)
    assert len(attempted) == 1 and round_.view.pages > 1


async def test_concurrent_votes_coalesce_into_one_latest_caption(rig, monkeypatch):
    monkeypatch.setattr(game, "EDIT_INTERVAL", 0.02)
    round_ = await start(rig)
    await asyncio.gather(*(click(rig, round_, i % 6, user_id=i) for i in range(40)))
    assert len(round_.votes) == 40
    assert len(edits(rig)) == 1
    assert "40" in edits(rig)[0].caption
    assert all(country not in edits(rig)[0].caption for country in round_.options)


async def test_finish_waits_for_an_inflight_active_edit_and_keeps_result_overview(rig, monkeypatch):
    round_ = await start(rig)
    seed(round_)
    entered, release = asyncio.Event(), asyncio.Event()
    request = rig.session.make_request

    async def blocked(bot, method, timeout=None):
        if isinstance(method, EditMessageCaption) and not entered.is_set():
            entered.set()
            await release.wait()
        return await request(bot, method, timeout)

    monkeypatch.setattr(rig.session, "make_request", blocked)
    paging = asyncio.create_task(click(rig, round_, "page_2"))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        closing = asyncio.create_task(click(rig, round_, "finish"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(paging, closing)
        assert "На снимке" in edits(rig)[-1].caption
        assert round_.page == 0
        assert all(
            button.callback_data.split(":")[-1].startswith("page_") for row in edits(rig)[-1].reply_markup.inline_keyboard for button in row
        )
        rig.client.eval.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(paging, *([closing] if closing else []), return_exceptions=True)
