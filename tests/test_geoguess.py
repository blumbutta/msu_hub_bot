import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from common.externals import geoguess as source
from common.externals.exceptions import ExternalServiceError
from hub_bot.commands import geoguess as game


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
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id), message_id=message_id, reply=AsyncMock(), edit_text=AsyncMock(), edit_caption=AsyncMock()
    )


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
    monkeypatch.setattr(game, "random_photo", AsyncMock(return_value=PHOTO))
    monkeypatch.setattr(game, "save_scores", AsyncMock())
    yield
    game.Geoguess.rounds = {}


def test_photo_buttons_no_timer_and_duplicate_command():
    async def scenario():
        m = message()
        await game.Geoguess.process(m)
        r = game.Geoguess.rounds[1]
        args = m.reply_photo.call_args
        assert args.args == (PHOTO.url,)
        assert "caption" not in args.kwargs
        buttons = [b for row in args.kwargs["reply_markup"].inline_keyboard for b in row]
        assert len(buttons) == 5
        assert len(set(r.options)) == 4 and PHOTO.country in r.options
        assert buttons[-1].text == "Завершить задание"
        assert r.task is None
        await game.Geoguess.process(m)
        assert m.reply.call_args.args[0] == "Подождите, прошлое задание еще не окончено!"
        assert game.random_photo.await_count == 1

    asyncio.run(scenario())


def test_loading_reserves_chat_and_other_chat_is_independent():
    async def scenario():
        loading = asyncio.Event()
        release = asyncio.Event()

        async def fetch():
            loading.set()
            await release.wait()
            return PHOTO

        game.random_photo.side_effect = fetch
        m = message()
        task = asyncio.create_task(game.Geoguess.process(m))
        await loading.wait()
        await game.Geoguess.process(m)
        assert "Подождите" in m.reply.call_args.args[0]
        release.set()
        await task
        await game.Geoguess.process(message(2))
        assert set(game.Geoguess.rounds) == {1, 2}

    asyncio.run(scenario())


def test_visible_vote_and_duplicate_vote_rejected():
    async def scenario():
        await game.Geoguess.process(message())
        r = game.Geoguess.rounds[1]
        await game.Geoguess.process_cb(*query(r, 0))
        assert r.votes == {5: (0, "User <5>")}
        text = r.board.edit_text.call_args.args[0]
        assert "User &lt;5&gt;" in text and r.options[0] in text
        q, data = query(r, 1)
        await game.Geoguess.process_cb(q, data)
        assert r.votes[5][0] == 0
        assert "уже принят" in q.answer.call_args.args[0]

    asyncio.run(scenario())


def test_anyone_can_finish_once_and_score_only_correct_votes():
    async def scenario():
        await game.Geoguess.process(message())
        r = game.Geoguess.rounds[1]
        answer = r.options.index(PHOTO.country)
        await game.Geoguess.process_cb(*query(r, answer, 10))
        await game.Geoguess.process_cb(*query(r, (answer + 1) % 4, 11))
        q1, d1 = query(r, "finish", 99)
        q2, d2 = query(r, "finish", 98)
        await asyncio.gather(game.Geoguess.process_cb(q1, d1), game.Geoguess.process_cb(q2, d2))
        assert game.Geoguess.rounds == {}
        game.save_scores.assert_awaited_once_with(1, [(10, "User <10>")])
        text = r.message.edit_caption.call_args.args[0]
        assert "Берген, Норвегия" in text and "+1 очко" in text
        assert r.message.edit_caption.call_args.kwargs["reply_markup"] is None
        assert "Источник фотографии" in text
        await game.Geoguess.process_cb(*query(r, answer, 12))
        assert 12 not in r.votes

    asyncio.run(scenario())


def test_no_votes_can_finish_and_start_next():
    async def scenario():
        await game.Geoguess.process(message())
        r = game.Geoguess.rounds[1]
        await game.Geoguess.process_cb(*query(r, "finish"))
        assert "никто не ответил" in r.message.edit_caption.call_args.args[0]
        await game.Geoguess.process(message())
        assert game.Geoguess.rounds[1].token != r.token

    asyncio.run(scenario())


def test_failed_fetch_releases_chat():
    game.random_photo.side_effect = ExternalServiceError("Источник недоступен")
    m = message()
    asyncio.run(game.Geoguess.process(m))
    assert game.Geoguess.rounds == {}
    m.reply_photo.assert_not_called()
    assert m.reply.call_args.args[0] == "Ошибка, попробуйте еще раз"


def test_old_message_and_invalid_choice():
    async def scenario():
        await game.Geoguess.process(message())
        r = game.Geoguess.rounds[1]
        q, data = query(r, 4)
        await game.Geoguess.process_cb(q, data)
        assert not r.votes
        q, data = query(r, 0)
        data["round"] = "old-token"
        await game.Geoguess.process_cb(q, data)
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


def test_score_storage_and_top(monkeypatch):
    import sys
    from types import ModuleType
    from unittest.mock import MagicMock

    # Load a fresh copy to exercise the real storage adapter, not fixture's stub.
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("geoguess_storage_test", Path(game.__file__))
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    pipe = MagicMock()
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock()
    pipe.execute = AsyncMock()
    client = SimpleNamespace(
        pipeline=MagicMock(return_value=pipe),
        zrevrange=AsyncMock(return_value=[(b"10", 3.0)]),
        hget=AsyncMock(return_value=b"Alice <name>"),
    )
    app = ModuleType("app")
    app.redis = SimpleNamespace(redis=AsyncMock(return_value=client))
    monkeypatch.setitem(sys.modules, "app", app)

    async def scenario():
        await module.save_scores(1, [(10, "Alice <name>")])
        pipe.zincrby.assert_called_once_with("msu_hub:geoguess:1:scores", 1, "10")
        pipe.execute.assert_awaited_once()
        m = message()
        await module.Geoguess.top(m)
        assert "Alice &lt;name&gt; — 3" in m.reply.call_args.args[0]
        client.zrevrange.assert_awaited_once_with("msu_hub:geoguess:1:scores", 0, 9, withscores=True)

    asyncio.run(scenario())


def test_all_voters_visible_with_large_group():
    async def scenario():
        await game.Geoguess.process(message())
        r = game.Geoguess.rounds[1]
        r.votes = {i: (i % 4, f"Participant {i:04d} " + "X" * 20) for i in range(150)}
        await game.Geoguess.update_board(r)
        assert len(r.board_texts) > 1
        rendered = "\n".join(r.board_texts)
        assert all(f"Participant {i:04d}" in rendered for i in range(150))
        assert all(len(text) <= 3000 for text in r.board_texts)

    asyncio.run(scenario())


def test_score_failure_still_reveals_answer():
    async def scenario():
        await game.Geoguess.process(message())
        r = game.Geoguess.rounds[1]
        r.votes[1] = (r.options.index(PHOTO.country), "Test")
        game.save_scores.side_effect = RuntimeError("storage unavailable")
        await game.Geoguess.process_cb(*query(r, "finish"))
        assert "Не удалось подтвердить запись очков" in r.message.edit_caption.call_args.args[0]
        assert not game.Geoguess.rounds

    asyncio.run(scenario())


def test_winners_use_usernames_and_clickable_fallback():
    async def scenario():
        await game.Geoguess.process(message())
        r = game.Geoguess.rounds[1]
        answer = r.options.index(PHOTO.country)
        await game.Geoguess.process_cb(*query(r, answer, 10))
        q, data = query(r, answer, 11)
        q.from_user.username = None
        await game.Geoguess.process_cb(q, data)
        await game.Geoguess.process_cb(*query(r, "finish"))
        text = r.message.edit_caption.call_args.args[0]
        assert "@user_10" in text
        assert '<a href="tg://user?id=11">User &lt;11&gt;</a>' in text

    asyncio.run(scenario())


def test_long_winner_list_mentions_everyone():
    async def scenario():
        await game.Geoguess.process(message())
        r = game.Geoguess.rounds[1]
        answer = r.options.index(PHOTO.country)
        r.votes = {i: (answer, f"User {i}") for i in range(100)}
        r.usernames = {i: f"participant_{i:04d}" for i in range(100)}
        await game.Geoguess.process_cb(*query(r, "finish"))
        text = r.message.edit_caption.call_args.args[0]
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
        await game.Geoguess.process(m)
        assert cancelled.is_set()
        assert not game.Geoguess.rounds
        assert m.reply.call_args.args[0] == "Ошибка, попробуйте еще раз"
        assert game.random_photo.await_count == 1

    asyncio.run(scenario())


def test_fetch_and_send_share_one_deadline(monkeypatch):
    monkeypatch.setattr(game, "PHOTO_TIMEOUT", 0.04)

    async def scenario():
        async def fetch():
            await asyncio.sleep(0.025)
            return PHOTO

        async def send(*args, **kwargs):
            await asyncio.sleep(0.025)
            return message_stub(1, 101)

        m = message()
        game.random_photo.side_effect = fetch
        m.reply_photo.side_effect = send
        await game.Geoguess.process(m)
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


@pytest.mark.parametrize("data", [{}, {"error": "Unable to geocode"}, {"address": {"country": "Atlantis", "country_code": "zz"}}])
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
        await game.Geoguess.process(message())
        r = game.Geoguess.rounds[1]
        await game.Geoguess.process_cb(*query(r, "finish"))
        text = r.message.edit_caption.call_args.args[0]
        assert "<b>Норвегия</b>" in text
        assert "OpenStreetMap" in text

    asyncio.run(scenario())
