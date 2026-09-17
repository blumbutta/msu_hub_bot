import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from aiogram.types import Message
from msu_hub_bot.telegram.runtime import Supervisor

import pytest

from msu_hub_bot.providers import geoguess as source
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.commands import geoguess as game


REAL_TODAY = game.today


PHOTO = source.Photo(
    "Норвегия",
    "Берген",
    "https://upload.wikimedia.org/test.jpg",
    "https://commons.wikimedia.org/?curid=1",
    "Author <name>",
    "CC BY 3.0",
    "https://creativecommons.org/licenses/by/3.0",
)


def message(chat_id=1, message_id=100):
    m = SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=message_id)
    m.reply_photo = AsyncMock(return_value=message_stub(chat_id, message_id + 1))
    m.reply = AsyncMock(return_value=message_stub(chat_id, message_id + 2))
    return m


def message_stub(chat_id, message_id):
    return Mock(
        spec=Message,
        chat=SimpleNamespace(id=chat_id),
        message_id=message_id,
        reply=AsyncMock(),
        edit_text=AsyncMock(),
        edit_caption=AsyncMock(),
    )


async def start(message):
    return await game.Geoguess.process(message, None, Supervisor())


async def process_callback(query, data):
    return await game.Geoguess.process_cb(query, game.GeoguessCallback(**data), None, Supervisor())


def query(round_, choice, user_id=5):
    q = SimpleNamespace(
        message=round_.message,
        from_user=SimpleNamespace(id=user_id, full_name=f"User <{user_id}>", username=f"user_{user_id}"),
        answer=AsyncMock(),
    )
    return q, {"round": round_.token, "choice": str(choice)}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    game.Geoguess.rounds = {}
    monkeypatch.setattr(game.Geoguess, "recent_countries", game.LRUCache(maxsize=1024))
    monkeypatch.setattr(game, "today", lambda: date(2026, 9, 17))

    async def send(method):
        return await method

    monkeypatch.setattr(game, "_send", send)
    monkeypatch.setattr(game, "random_photo", AsyncMock(return_value=PHOTO))
    monkeypatch.setattr(game, "save_scores", AsyncMock())
    yield
    game.Geoguess.rounds = {}


def test_photo_has_six_flagged_choices_timer_and_duplicate_guard():
    async def scenario():
        m = message()
        await start(m)
        r = game.Geoguess.rounds[1]
        args = m.reply_photo.call_args
        assert args.args == (PHOTO.url,)
        assert "caption" not in args.kwargs
        buttons = [b for row in args.kwargs["reply_markup"].inline_keyboard for b in row]
        assert len(buttons) == 7
        assert len(set(r.options)) == 6 and PHOTO.country in r.options
        assert [button.text for button in buttons[:-1]] == [game.country_label(country) for country in r.options]
        assert "🇳🇴 Норвегия" in [button.text for button in buttons]
        assert buttons[-1].text == "Завершить задание"
        assert r.task is None
        assert r.timer is not None and not r.timer.cancelled()
        assert game.ROUND_TIMEOUT == 600
        await start(m)
        assert m.reply.call_args.args[0] == "Подождите, прошлое задание еще не окончено!"
        assert game.random_photo.await_count == 1

    asyncio.run(scenario())


def test_loading_reserves_chat_and_other_chat_is_independent():
    async def scenario():
        loading = asyncio.Event()
        release = asyncio.Event()

        async def fetch(recent_countries):
            loading.set()
            await release.wait()
            return PHOTO

        game.random_photo.side_effect = fetch
        m = message()
        task = asyncio.create_task(start(m))
        await loading.wait()
        await start(m)
        assert "Подождите" in m.reply.call_args.args[0]
        release.set()
        await task
        await start(message(2))
        assert set(game.Geoguess.rounds) == {1, 2}

    asyncio.run(scenario())


def test_active_votes_hide_choices_but_show_count_names_and_usernames():
    async def scenario():
        await start(message())
        r = game.Geoguess.rounds[1]
        await process_callback(*query(r, 0))
        assert r.votes == {5: (0, "User <5>")}
        text = r.board.edit_text.call_args.args[0]
        assert "User &lt;5&gt;" in text and "@user_5" in text
        assert "1" in text
        assert all(country not in text for country in r.options)
        q, data = query(r, 1)
        await process_callback(q, data)
        assert r.votes[5][0] == 0
        assert "уже принят" in q.answer.call_args.args[0]

    asyncio.run(scenario())


def test_anyone_can_finish_once_and_scores_all_votes():
    async def scenario():
        await start(message())
        r = game.Geoguess.rounds[1]
        answer = r.options.index(PHOTO.country)
        await process_callback(*query(r, answer, 10))
        await process_callback(*query(r, (answer + 1) % 6, 11))
        q1, d1 = query(r, "finish", 99)
        q2, d2 = query(r, "finish", 98)
        await asyncio.gather(process_callback(q1, d1), process_callback(q2, d2))
        assert game.Geoguess.rounds == {}
        game.save_scores.assert_awaited_once_with(
            1, [(10, "User <10>", "user_10", 1), (11, "User <11>", "user_11", -1)], None, date(2026, 9, 17), round_token=r.token
        )
        final_board = "\n".join(r.board_texts)
        assert all(game.country_label(country) in final_board for country in r.options)
        assert "User &lt;10&gt;" in final_board and "@user_10" in final_board
        assert "User &lt;11&gt;" in final_board and "@user_11" in final_board
        assert r.timer.cancelled()
        text = r.message.edit_caption.call_args.kwargs["caption"]
        assert "Берген" in text and "Норвегия" in text and "+1 очко" in text
        assert r.message.edit_caption.call_args.kwargs["reply_markup"] is None
        assert "Источник фотографии" in text
        await process_callback(*query(r, answer, 12))
        assert 12 not in r.votes

    asyncio.run(scenario())


def test_no_votes_can_finish_and_start_next():
    async def scenario():
        await start(message())
        r = game.Geoguess.rounds[1]
        await process_callback(*query(r, "finish"))
        assert "никто не ответил" in r.message.edit_caption.call_args.kwargs["caption"]
        await start(message())
        assert game.Geoguess.rounds[1].token != r.token

    asyncio.run(scenario())


def test_failed_fetch_releases_chat():
    game.random_photo.side_effect = ExternalServiceError("Источник недоступен")
    m = message()
    asyncio.run(start(m))
    assert game.Geoguess.rounds == {}
    m.reply_photo.assert_not_called()
    assert m.reply.call_args.args[0] == "Ошибка, попробуйте еще раз"


def test_old_message_and_invalid_choice():
    async def scenario():
        await start(message())
        r = game.Geoguess.rounds[1]
        q, data = query(r, 6)
        await process_callback(q, data)
        assert not r.votes
        q, data = query(r, 0)
        data["round"] = "old-token"
        await process_callback(q, data)
        assert not r.votes

    asyncio.run(scenario())


def sample():
    return {
        "query": {
            "pages": {
                "1": {
                    "pageid": 1,
                    "imageinfo": [
                        {
                            "mime": "image/jpeg",
                            "width": 1000,
                            "height": 800,
                            "url": PHOTO.url,
                            "extmetadata": {
                                k: {"value": v}
                                for k, v in {
                                    "GPSLatitude": "60.397",
                                    "GPSLongitude": "5.325",
                                    "Artist": '<a href="https://example.com">Alice &amp; Bob</a>',
                                    "LicenseShortName": "CC BY 3.0",
                                    "LicenseUrl": PHOTO.license_url,
                                }.items()
                            },
                        }
                    ],
                }
            }
        }
    }


def test_geography_author_license():
    result = source.candidates(sample())
    assert len(result) == 1
    assert result[0].photo.author == "Alice & Bob"
    assert result[0].latitude == 60.397
    assert result[0].longitude == 5.325
    assert result[0].photo.country == ""


@pytest.mark.parametrize("field,value", [("mime", "image/svg+xml"), ("width", 40), ("url", "https://evil.example/x")])
def test_unsuitable_media_filtered(field, value):
    data = sample()
    data["query"]["pages"]["1"]["imageinfo"][0][field] = value
    assert source.candidates(data) == []


def test_invalid_location_filtered():
    data = sample()
    data["query"]["pages"]["1"]["imageinfo"][0]["extmetadata"]["GPSLatitude"]["value"] = "nan"
    assert source.candidates(data) == []


@pytest.mark.parametrize("field", ["LicenseUrl", "Artist", "GPSLatitude"])
@pytest.mark.parametrize("value", [None, 12, {"unexpected": "value"}])
def test_malformed_metadata_does_not_discard_valid_later_photo(field, value):
    import copy

    data = sample()
    pages = data["query"]["pages"]
    pages["2"] = copy.deepcopy(pages["1"])
    pages["2"]["pageid"] = 2
    pages["1"]["imageinfo"][0]["extmetadata"][field]["value"] = value
    result = source.candidates(data)
    assert len(result) == 1
    assert result[0].photo.source.endswith("curid=2")


@pytest.mark.parametrize("data", [None, {"query": None}, {"query": {"pages": []}}])
def test_malformed_photo_response_has_no_candidates(data):
    assert source.candidates(data) == []


@pytest.mark.parametrize(
    "width,height,accepted",
    [(960, 768, True), (5000, 5000, True), (5001, 5000, False), (400, 8000, True), (400, 8001, False), (None, 768, False)],
)
def test_selected_thumbnail_respects_telegram_dimensions(width, height, accepted):
    data = sample()
    info = data["query"]["pages"]["1"]["imageinfo"][0]
    info.update(width=6000, height=6000, thumburl="https://upload.wikimedia.org/thumb.jpg", thumbwidth=width, thumbheight=height)
    result = source.candidates(data)
    assert bool(result) is accepted
    if accepted:
        assert result[0].photo.url == info["thumburl"]


@pytest.mark.parametrize("original_width,original_height,accepted", [(1000, 800, True), (6000, 6000, False), (600, 12600, False)])
def test_missing_thumbnail_dimensions_fall_back_to_original(original_width, original_height, accepted):
    data = sample()
    info = data["query"]["pages"]["1"]["imageinfo"][0]
    info.update(width=original_width, height=original_height, thumburl="https://upload.wikimedia.org/thumb.jpg")
    assert bool(source.candidates(data)) is accepted


def test_daily_score_storage_and_top(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    # Exercise the real adapter without the game fixture's save_scores stub.
    spec = importlib.util.spec_from_file_location("geoguess_storage_test", Path(game.__file__))
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    day = date(2026, 9, 17)
    monkeypatch.setattr(module, "today", lambda: day)
    client = SimpleNamespace(
        eval=AsyncMock(return_value=1),
        zrevrange=AsyncMock(return_value=[(b"10", 3.0)]),
        hget=AsyncMock(side_effect=[b"Alice <name>", b"alice"]),
    )
    redis = SimpleNamespace(redis=AsyncMock(return_value=client))

    async def send(method):
        return await method

    module._send = send

    async def scenario():
        await module.save_scores(1, [(10, "Alice <name>", "alice", 1)], redis, day=day, round_token="synthetic-round")
        client.eval.assert_awaited_once()
        args = client.eval.call_args.args
        assert module.score_key(1, day) in args
        assert module.score_key(1, day) + ":names" in args
        assert module.score_key(1, day) + ":usernames" in args
        assert module.score_key(1, day) + ":rounds" in args
        assert args[1] == 4 and "synthetic-round" in args
        m = message()
        await module.Geoguess.top(m, redis)
        text = m.reply.call_args.args[0]
        assert "Alice &lt;name&gt;" in text and "@alice" in text and "— 3" in text
        assert "17.09.2026" in text
        client.zrevrange.assert_awaited_once_with(module.score_key(1, day), 0, 9, withscores=True)
        assert module.score_key(1, day) != module.score_key(1, date(2026, 9, 16))
        assert module.score_key(1, day) != "msu_hub:geoguess:1:scores"

    asyncio.run(scenario())


def test_all_voters_visible_with_large_group():
    async def scenario():
        await start(message())
        r = game.Geoguess.rounds[1]
        r.votes = {i: (i % 6, f"Participant {i:04d} " + "X" * 20) for i in range(150)}
        await game.Geoguess.update_board(r)
        assert len(r.board_texts) > 1
        rendered = "\n".join(r.board_texts)
        assert all(f"Participant {i:04d}" in rendered for i in range(150))
        assert all(len(text) <= 3000 for text in r.board_texts)

    asyncio.run(scenario())


def test_score_failure_still_reveals_answer():
    async def scenario():
        await start(message())
        r = game.Geoguess.rounds[1]
        r.votes[1] = (r.options.index(PHOTO.country), "Test")
        game.save_scores.side_effect = RuntimeError("storage unavailable")
        await process_callback(*query(r, "finish"))
        assert "Не удалось подтвердить запись очков" in r.message.edit_caption.call_args.kwargs["caption"]
        assert not game.Geoguess.rounds

    asyncio.run(scenario())


def test_winners_use_usernames_and_clickable_fallback():
    async def scenario():
        await start(message())
        r = game.Geoguess.rounds[1]
        answer = r.options.index(PHOTO.country)
        await process_callback(*query(r, answer, 10))
        q, data = query(r, answer, 11)
        q.from_user.username = None
        await process_callback(q, data)
        await process_callback(*query(r, "finish"))
        text = r.message.edit_caption.call_args.kwargs["caption"]
        assert "@user_10" in text and "User &lt;10&gt;" in text
        assert '<a href="tg://user?id=11">User &lt;11&gt;</a>' in text

    asyncio.run(scenario())


def test_long_winner_list_mentions_everyone():
    async def scenario():
        await start(message())
        r = game.Geoguess.rounds[1]
        answer = r.options.index(PHOTO.country)
        r.votes = {i: (answer, f"User {i}") for i in range(100)}
        r.usernames = {i: f"participant_{i:04d}" for i in range(100)}
        await process_callback(*query(r, "finish"))
        text = r.message.edit_caption.call_args.kwargs["caption"]
        text += "\n".join(call.args[0] for call in r.message.reply.call_args_list)
        assert all("@participant_" + f"{i:04d}" in text for i in range(100))

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["fetch", "send"])
def test_total_photo_deadline_cancels_and_unlocks(monkeypatch, stage):
    monkeypatch.setattr(game, "PHOTO_TIMEOUT", 0.02)

    async def scenario():
        cancelled = asyncio.Event()

        async def blocked(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        m = message()
        if stage == "fetch":
            game.random_photo.side_effect = blocked
        else:
            m.reply_photo.side_effect = blocked
        await start(m)
        assert cancelled.is_set()
        assert not game.Geoguess.rounds
        assert m.reply.call_args.args[0] == "Ошибка, попробуйте еще раз"
        assert game.random_photo.await_count == 1

    asyncio.run(scenario())


def test_fetch_and_send_share_one_deadline(monkeypatch):
    monkeypatch.setattr(game, "PHOTO_TIMEOUT", 0.04)

    async def scenario():
        async def fetch(recent_countries):
            await asyncio.sleep(0.025)
            return PHOTO

        async def send(*args, **kwargs):
            await asyncio.sleep(0.025)
            return message_stub(1, 101)

        m = message()
        game.random_photo.side_effect = fetch
        m.reply_photo.side_effect = send
        await start(m)
        assert not game.Geoguess.rounds
        assert m.reply.call_args.args[0] == "Ошибка, попробуйте еще раз"

    asyncio.run(scenario())


@pytest.mark.parametrize("latitude,longitude", [(0, 0), (-33.9, 18.4), (27.7, 85.3), (-17.7, 178.4)])
def test_coordinates_worldwide_are_not_restricted(latitude, longitude):
    data = sample()
    metadata = data["query"]["pages"]["1"]["imageinfo"][0]["extmetadata"]
    metadata["GPSLatitude"]["value"] = str(latitude)
    metadata["GPSLongitude"]["value"] = str(longitude)
    assert len(source.candidates(data)) == 1


def test_country_uses_code_not_untrusted_display_name():
    assert source.location({"address": {"country_code": "np", "country": "Nepal", "city": "Катманду"}}) == ("Непал", "Катманду")
    assert len(source.COUNTRIES) > 200


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"error": "Unable to geocode"},
        {"address": None},
        {"address": []},
        {"address": {"country": "Atlantis", "country_code": "zz"}},
        {"address": {"country": "Nepal", "country_code": None}},
        {"address": {"country": 123, "country_code": "np"}},
        {"address": {"country": "Nepal", "country_code": "np", "city": 123}},
    ],
)
def test_unknown_country_rejected(data):
    with pytest.raises(source.UnknownLocation):
        source.location(data)


def test_unknown_photo_skipped_before_delivery(monkeypatch):
    import copy

    data = sample()
    second = copy.deepcopy(data["query"]["pages"]["1"])
    second["pageid"] = 2
    data["query"]["pages"]["2"] = second
    monkeypatch.setattr(source.random, "shuffle", lambda photos: None)
    fetch = AsyncMock(return_value=data)
    reverse = AsyncMock(side_effect=[source.UnknownLocation("unknown"), ("Непал", "Катманду")])
    monkeypatch.setattr(source, "request_json", fetch)
    monkeypatch.setattr(source, "reverse_location", reverse)
    photo = asyncio.run(source.random_photo())
    assert photo.country == "Непал" and photo.source.endswith("curid=2")
    params = fetch.call_args.args[2]
    assert params["generator"] == "random" and params["grnnamespace"] == 6
    assert "gsrsearch" not in params and "ggscoord" not in params
    assert reverse.await_count == 2


def test_malformed_geocoder_location_skips_to_next_photo(monkeypatch):
    import copy

    data = sample()
    second = copy.deepcopy(data["query"]["pages"]["1"])
    second["pageid"] = 2
    data["query"]["pages"]["2"] = second
    monkeypatch.setattr(source.random, "shuffle", lambda photos: None)
    monkeypatch.setattr(source, "_geocoder_lock", None)
    monkeypatch.setattr(source, "_geocoder_next", 0)
    monkeypatch.setattr(source, "_geocoder_cache", {})
    fetch = AsyncMock(side_effect=[data, {"address": None}, {"address": {"country": "Nepal", "country_code": "np"}}])
    monkeypatch.setattr(source, "request_json", fetch)
    photo = asyncio.run(source.random_photo())
    assert photo.country == "Непал" and photo.source.endswith("curid=2")
    assert fetch.await_count == 3


def test_no_country_means_no_photo(monkeypatch):
    monkeypatch.setattr(source, "request_json", AsyncMock(return_value=sample()))
    monkeypatch.setattr(source, "reverse_location", AsyncMock(side_effect=source.UnknownLocation("unknown")))
    with pytest.raises(ExternalServiceError):
        asyncio.run(source.random_photo())


def test_geocoder_cache_and_throttle(monkeypatch):
    monkeypatch.setattr(source, "_geocoder_lock", None)
    monkeypatch.setattr(source, "_geocoder_next", 0)
    monkeypatch.setattr(source, "_geocoder_cache", {})
    request = AsyncMock(return_value={"address": {"country_code": "np", "country": "Nepal"}})
    monkeypatch.setattr(source, "request_json", request)
    starts = []

    async def record(*args):
        starts.append(source.time.monotonic())
        return {"address": {"country_code": "np", "country": "Nepal"}}

    request.side_effect = record

    async def scenario():
        await asyncio.gather(source.reverse_location(None, 27, 85), source.reverse_location(None, 28, 85))
        await source.reverse_location(None, 27, 85)

    asyncio.run(scenario())
    assert request.await_count == 2
    assert starts[1] - starts[0] >= 1


def test_country_only_caption_has_no_empty_city():
    async def scenario():
        from dataclasses import replace

        game.random_photo.return_value = replace(PHOTO, city="")
        await start(message())
        r = game.Geoguess.rounds[1]
        await process_callback(*query(r, "finish"))
        text = r.message.edit_caption.call_args.kwargs["caption"]
        assert "Норвегия</b>" in text
        assert "OpenStreetMap" in text

    asyncio.run(scenario())


def test_country_and_user_labels_escape_untrusted_text():
    assert game.country_label("Норвегия") == "🇳🇴 Норвегия"
    assert game.country_label("Unlisted <place>") == "Unlisted <place>"
    label = game.user_label(42, "User <name>", "user_name")
    assert "User &lt;name&gt;" in label and "@user_name" in label
    assert game.user_label(42, "User <name>", None) == '<a href="tg://user?id=42">User &lt;name&gt;</a>'


def test_today_uses_moscow_midnight(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 16, 21, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(game, "datetime", Clock)
    assert REAL_TODAY() == date(2026, 9, 17)


def test_scoring_uses_day_when_round_finishes(monkeypatch):
    async def scenario():
        day = date(2026, 9, 16)
        monkeypatch.setattr(game, "today", lambda: day)
        await start(message())
        round_ = game.Geoguess.rounds[1]
        await process_callback(*query(round_, round_.options.index(PHOTO.country), 10))
        day = date(2026, 9, 17)
        await process_callback(*query(round_, "finish"))
        assert game.save_scores.call_args.args[3] == day

    asyncio.run(scenario())


def test_failed_loser_penalty_is_reported_even_with_no_winners():
    async def scenario():
        await start(message())
        round_ = game.Geoguess.rounds[1]
        wrong = next(index for index, country in enumerate(round_.options) if country != PHOTO.country)
        await process_callback(*query(round_, wrong))
        game.save_scores.side_effect = RuntimeError("storage unavailable")
        await process_callback(*query(round_, "finish"))
        assert game.save_scores.call_args.args[1] == [(5, "User <5>", "user_5", -1)]
        result = round_.message.edit_caption.call_args.kwargs["caption"]
        assert "Не удалось подтвердить запись очков" in result
        assert "Норвегия" in result and not game.Geoguess.rounds

    asyncio.run(scenario())


def test_recent_countries_follow_last_fifteen_delivered_photos_per_chat():
    from dataclasses import replace

    async def scenario():
        countries = ["Норвегия", "Непал", "Япония", "Норвегия", "Франция", "Италия"] * 3
        for country in countries:
            game.random_photo.return_value = replace(PHOTO, country=country)
            await game.Geoguess.send_round_photo(message(1), game.Round("test"))
        history = tuple(countries[-15:])
        assert game.Geoguess.recent_countries[1] == history
        game.random_photo.return_value = PHOTO
        await game.Geoguess.send_round_photo(message(2), game.Round("other"))
        assert game.random_photo.call_args.args == ((),)
        assert game.Geoguess.recent_countries[1] == history
        assert game.Geoguess.recent_countries[2] == ("Норвегия",)
        await game.Geoguess.send_round_photo(message(1), game.Round("next"))
        assert game.random_photo.call_args.args == (history,)
        assert game.Geoguess.recent_countries[1] == (*history, "Норвегия")[-15:]

    asyncio.run(scenario())


def test_failed_delivery_keeps_country_history(monkeypatch):
    monkeypatch.setattr(game, "PHOTO_TIMEOUT", 0.02)

    async def scenario():
        history = ("Непал", "Франция")
        game.Geoguess.recent_countries[1] = history
        m = message()

        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()

        m.reply_photo.side_effect = blocked
        await start(m)
        assert game.random_photo.call_args.args == (history,)
        assert game.Geoguess.recent_countries[1] == history
        assert not game.Geoguess.rounds
        assert m.reply.call_args.args[0] == "Ошибка, попробуйте еще раз"

    asyncio.run(scenario())


def test_shutdown_discards_only_in_memory_country_history():
    async def scenario():
        game.Geoguess.recent_countries[1] = ("Непал", "Франция")
        await game.Geoguess.shutdown()
        assert not game.Geoguess.recent_countries

    asyncio.run(scenario())
