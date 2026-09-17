import asyncio
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from msu_hub_bot.providers import geoguess as source
from msu_hub_bot.providers.exceptions import ExternalServiceError


@pytest.fixture
def provider(monkeypatch):
    pages = {
        str(index): {
            "pageid": index,
            "imageinfo": [
                {
                    "mime": "image/jpeg",
                    "width": 960,
                    "height": 800,
                    "url": f"https://upload.wikimedia.org/{index}.jpg",
                    "extmetadata": {
                        key: {"value": value}
                        for key, value in {
                            "GPSLatitude": str(index),
                            "GPSLongitude": "10",
                            "Artist": "Example photographer",
                            "LicenseShortName": "CC BY 3.0",
                            "LicenseUrl": "https://creativecommons.org/licenses/by/3.0",
                        }.items()
                    },
                }
            ],
        }
        for index in range(1, 7)
    }
    session = Mock(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
    session.__aenter__.return_value = session
    monkeypatch.setattr(source.aiohttp, "ClientSession", Mock(return_value=session))
    monkeypatch.setattr(source, "request_json", AsyncMock(return_value={"query": {"pages": pages}}))
    monkeypatch.setattr(source.random, "shuffle", Mock())
    monkeypatch.setattr(source, "reverse_location", AsyncMock())
    return session


def test_prefers_new_country_without_restricting_commons_sample(provider):
    source.reverse_location.side_effect = [
        ("Норвегия", "Берген"),
        source.UnknownLocation("No country"),
        ("Непал", "Катманду"),
        ("Франция", "Париж"),
    ]

    photo = asyncio.run(source.random_photo(("Норвегия", "Франция")))

    assert (photo.country, photo.city) == ("Непал", "Катманду")
    assert photo.url.endswith("/3.jpg")
    assert source.reverse_location.await_count == 3
    source.request_json.assert_awaited_once()
    params = source.request_json.call_args.args[2]
    assert params["generator"] == "random" and params["grnnamespace"] == 6
    assert params["grnlimit"] == 30
    assert not any("country" in key or "search" in key for key in params)
    provider.__aexit__.assert_awaited_once()


def test_no_history_returns_first_verified_photo_without_extra_geocoding(provider):
    source.reverse_location.side_effect = [("Норвегия", "Берген"), AssertionError("Extra request")]

    photo = asyncio.run(source.random_photo())

    assert photo.country == "Норвегия" and photo.url.endswith("/1.jpg")
    assert source.reverse_location.await_count == 1
    provider.__aexit__.assert_awaited_once()


def test_all_recent_uses_latest_occurrence_so_duplicates_cannot_hide_recent_repeats(provider):
    history = ("Норвегия", "Франция", "Япония", "Франция", "Норвегия")
    source.reverse_location.side_effect = [
        ("Франция", "Париж"),
        ("Норвегия", "Берген"),
        ("Япония", "Киото"),
        ("Франция", "Лион"),
    ]

    photo = asyncio.run(source.random_photo(history))

    assert (photo.country, photo.city) == ("Япония", "Киото")
    assert source.reverse_location.await_count == 4


def test_equal_country_priority_keeps_first_shuffled_candidate_and_caps_requests(provider):
    source.reverse_location.side_effect = [
        ("Норвегия", "Берген"),
        ("Норвегия", "Осло"),
        source.UnknownLocation("No country"),
        source.UnknownLocation("No country"),
        ("Непал", "Катманду"),
    ]

    photo = asyncio.run(source.random_photo(("Норвегия",)))

    assert (photo.country, photo.city) == ("Норвегия", "Берген")
    assert source.reverse_location.await_count == 4


@pytest.mark.parametrize(
    "error",
    [TimeoutError(), ExternalServiceError("Unavailable"), aiohttp.ClientConnectionError(), ValueError("Malformed response")],
)
def test_later_provider_failure_returns_verified_fallback_and_closes_session(provider, error):
    source.reverse_location.side_effect = [("Норвегия", "Берген"), error]

    photo = asyncio.run(source.random_photo(("Норвегия",)))

    assert photo.country == "Норвегия"
    assert photo.url.endswith("/1.jpg")
    provider.__aexit__.assert_awaited_once()


def test_whole_operation_timeout_preserves_fallback_and_cleans_up(provider, monkeypatch):
    monkeypatch.setattr(source, "FETCH_TIMEOUT", 0.02)

    async def scenario():
        cancelled = asyncio.Event()

        async def geocode(session, latitude, longitude):
            if latitude == 1:
                return "Норвегия", "Берген"
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        source.reverse_location.side_effect = geocode
        photo = await source.random_photo(("Норвегия",))
        assert photo.country == "Норвегия"
        assert cancelled.is_set()
        provider.__aexit__.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("blocked_stage", ["commons", "geocoder"])
def test_timeout_without_verified_fallback_reports_error(provider, monkeypatch, blocked_stage):
    monkeypatch.setattr(source, "FETCH_TIMEOUT", 0.02)

    async def scenario():
        cancelled = asyncio.Event()

        async def blocked(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        target = source.request_json if blocked_stage == "commons" else source.reverse_location
        target.side_effect = blocked
        with pytest.raises(ExternalServiceError) as exc:
            await source.random_photo(("Норвегия",))
        assert isinstance(exc.value.__cause__, TimeoutError)
        assert cancelled.is_set()
        provider.__aexit__.assert_awaited_once()

    asyncio.run(scenario())


def test_caller_cancellation_wins_even_when_a_verified_fallback_exists(provider):
    async def scenario():
        finding_next = asyncio.Event()
        cancelled = asyncio.Event()

        async def geocode(session, latitude, longitude):
            if latitude == 1:
                return "Норвегия", "Берген"
            finding_next.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        source.reverse_location.side_effect = geocode
        task = asyncio.create_task(source.random_photo(("Норвегия",)))
        await finding_next.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        provider.__aexit__.assert_awaited_once()

    asyncio.run(scenario())


def test_unknown_countries_never_become_a_fallback(provider):
    source.reverse_location.side_effect = source.UnknownLocation("No country")

    with pytest.raises(ExternalServiceError, match="Нет фото с определённой страной"):
        asyncio.run(source.random_photo(("Норвегия",)))

    assert source.reverse_location.await_count == 4
    provider.__aexit__.assert_awaited_once()


def test_failure_without_fallback_keeps_provider_error(provider):
    error = ExternalServiceError("Unavailable")
    source.reverse_location.side_effect = error

    with pytest.raises(ExternalServiceError) as exc:
        asyncio.run(source.random_photo(("Норвегия",)))

    assert exc.value is error
    provider.__aexit__.assert_awaited_once()
