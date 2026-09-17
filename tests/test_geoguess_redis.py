"""Real Lua contracts; opt in with HUB_TEST_REDIS_URL pointing to local Redis."""

import asyncio
import os
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from msu_hub_bot.commands import geoguess as game
from msu_hub_bot.telegram.runtime import Supervisor
from msu_hub_bot.telegram.storage import RedisStorage

pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1", "localhost"])


@pytest.fixture
async def scores(monkeypatch):
    url = os.environ.get("HUB_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set HUB_TEST_REDIS_URL for real GeoGuess Redis contracts")
    target = urlsplit(url)
    if target.scheme != "redis" or target.hostname not in {"127.0.0.1", "::1", "localhost"}:
        pytest.fail("GeoGuess Redis contracts require a loopback Redis endpoint")
    client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
    namespace = f"hub_test_geoguess:{uuid4().hex}:"
    original_key = game.score_key
    keys = set()

    def score_key(chat_id, day=None):
        key = namespace + original_key(chat_id, day)
        keys.update((key, key + ":names", key + ":usernames", key + ":rounds"))
        return key

    monkeypatch.setattr(game, "score_key", score_key)
    try:
        await client.ping()
        yield SimpleNamespace(
            client=client,
            storage=RedisStorage(client, prefix=namespace, supervisor=Supervisor()),
            day=game.today(),
            key=score_key,
        )
    finally:
        try:
            if keys:
                # Exact UUID-owned keys only: other data never needs to be cleared.
                await client.delete(*keys)
        finally:
            await client.aclose()


async def test_daily_scores_floor_at_zero_and_refresh_player_labels(scores):
    await game.save_scores(
        101,
        [(201, "Игрок <один> 🧭", "synthetic_one", 1), (202, "Игрок два", None, -1)],
        scores.storage,
        scores.day,
        round_token="first",
    )
    key = scores.key(101, scores.day)
    assert await scores.client.zscore(key, "201") == 1
    assert await scores.client.zscore(key, "202") == 0
    assert await scores.client.hget(key + ":names", "201") == "Игрок <один> 🧭"
    assert await scores.client.hget(key + ":usernames", "201") == "synthetic_one"
    await game.save_scores(
        101,
        [(201, "Новое имя", None, -1), (202, "Игрок два", None, -1)],
        scores.storage,
        scores.day,
        round_token="second",
    )
    assert await scores.client.zscore(key, "201") == 0
    assert await scores.client.zscore(key, "202") == 0
    assert await scores.client.hget(key + ":names", "201") == "Новое имя"
    assert await scores.client.hget(key + ":usernames", "201") == ""


async def test_concurrent_rounds_and_duplicate_replays_preserve_every_score(scores):
    await asyncio.gather(
        *(
            game.save_scores(101, [(201, "Synthetic player", None, 1)], scores.storage, scores.day, round_token=f"round-{index % 12}")
            for index in range(48)
        )
    )
    key = scores.key(101, scores.day)
    assert await scores.client.zscore(key, "201") == 12
    assert await scores.client.scard(key + ":rounds") == 12


async def test_response_lost_after_commit_can_be_replayed_without_double_scoring(scores, monkeypatch):
    original_eval = scores.client.eval

    async def lose_response(*args, **kwargs):
        await original_eval(*args, **kwargs)
        raise TimeoutError("Synthetic lost response after Redis committed")

    with monkeypatch.context() as lost:
        lost.setattr(scores.client, "eval", lose_response)
        with pytest.raises(TimeoutError, match="Synthetic lost response"):
            await game.save_scores(101, [(201, "Synthetic player", None, 1)], scores.storage, scores.day, round_token="same-round")
    await game.save_scores(101, [(201, "Synthetic player", None, 1)], scores.storage, scores.day, round_token="same-round")
    key = scores.key(101, scores.day)
    assert await scores.client.zscore(key, "201") == 1
    assert await scores.client.scard(key + ":rounds") == 1


async def test_chat_and_day_records_are_independent_and_all_expire_together(scores):
    tomorrow = scores.day + timedelta(days=1)
    for chat_id, day, token in [
        (101, scores.day, "first"),
        (102, scores.day, "first"),
        (101, tomorrow, "first"),
        (101, scores.day, "second"),
    ]:
        await game.save_scores(chat_id, [(201, "Synthetic player", None, 1)], scores.storage, day, round_token=token)
    for chat_id, day, expected in [(101, scores.day, 2), (102, scores.day, 1), (101, tomorrow, 1)]:
        key = scores.key(chat_id, day)
        assert await scores.client.zscore(key, "201") == expected
        expiry = int(datetime.combine(day + timedelta(days=2), time.min, game.DAY_ZONE).timestamp())
        for suffix in ("", ":names", ":usernames", ":rounds"):
            assert await scores.client.expiretime(key + suffix) == expiry


async def test_round_without_players_creates_no_score_records(scores):
    await game.save_scores(101, [], scores.storage, scores.day, round_token="empty-round")
    key = scores.key(101, scores.day)
    assert await scores.client.exists(key, key + ":names", key + ":usernames", key + ":rounds") == 0
