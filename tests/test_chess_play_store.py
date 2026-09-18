"""Exercise the actual Redis CAS scripts, not a second implementation of them."""

import asyncio
import os
from time import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from msu_hub_bot.commands.chess_play_game import Game, Player
from msu_hub_bot.telegram import chess_play_store as store
from msu_hub_bot.telegram.runtime import Supervisor
from msu_hub_bot.telegram.storage import RedisStorage

pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1", "localhost"])


@pytest.fixture
async def rig():
    url = os.environ.get("HUB_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set HUB_TEST_REDIS_URL to exercise real match persistence")
    if urlsplit(url).hostname not in {"127.0.0.1", "::1", "localhost"}:
        pytest.fail("Match tests require loopback Redis")
    client = Redis.from_url(url, decode_responses=True)
    namespace = f"hub_test_chess_play:{uuid4().hex}"
    redis = RedisStorage(client, prefix=namespace, supervisor=Supervisor())
    try:
        await client.ping()
        yield SimpleNamespace(client=client, redis=redis, namespace=namespace)
    finally:
        keys = [key async for key in client.scan_iter(match=f"{namespace}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


def saved(*, bot=1, chat=-100, token="a" * 12):
    now = time()
    return store.SavedGame(
        game=Game(
            token=token,
            bot_id=bot,
            chat_id=chat,
            message_id=10,
            white=Player(user_id=21, name="Synthetic <player>", username="white"),
            created_at=now,
            invite_deadline=now + 600,
        )
    )


async def test_simultaneous_creations_accept_only_one_per_chat(rig):
    one, two = saved(), saved(token="b" * 12)
    result = await asyncio.gather(store.save(rig.redis, one), store.save(rig.redis, two), return_exceptions=True)
    assert sum(value is None for value in result) == 1
    assert sum(isinstance(value, store.Conflict) for value in result) == 1
    active = await store.active(rig.redis, 1, -100)
    assert active in (one, two)
    assert await store.pending(rig.redis, 1) == [active]


async def test_simultaneous_joins_never_replace_the_winner(rig):
    original = saved()
    await store.save(rig.redis, original)
    one, two = original.model_copy(deep=True), original.model_copy(deep=True)
    one.game.join(Player(user_id=22, name="One"), time())
    two.game.join(Player(user_id=23, name="Two"), time())
    result = await asyncio.gather(store.save(rig.redis, one, original), store.save(rig.redis, two, original), return_exceptions=True)
    assert sum(value is None for value in result) == 1
    assert sum(isinstance(value, store.Conflict) for value in result) == 1
    assert await store.active(rig.redis, 1, -100) in (one, two)


async def test_replaying_response_lost_after_commit_is_idempotent(rig, monkeypatch):
    original = saved()
    await store.save(rig.redis, original)
    joined = original.model_copy(deep=True)
    joined.game.join(Player(user_id=22, name="Black"), time())
    evaluate = rig.client.eval

    async def lose(*args):
        await evaluate(*args)
        raise TimeoutError("lost response")

    with monkeypatch.context() as patch:
        patch.setattr(rig.client, "eval", lose)
        with pytest.raises(TimeoutError):
            await store.save(rig.redis, joined, original)
    await store.save(rig.redis, joined, original)
    assert await store.active(rig.redis, 1, -100) == joined


async def test_finished_delivery_does_not_clear_a_new_games_slot(rig):
    original = saved()
    await store.save(rig.redis, original)
    ended = original.model_copy(deep=True)
    ended.game.cancel(21, time())
    await store.save(rig.redis, ended, original)
    assert await store.active(rig.redis, 1, -100) is None
    fresh = saved(token="b" * 12)
    await store.save(rig.redis, fresh)
    delivered = ended.model_copy(update={"revealed": True})
    await store.save(rig.redis, delivered, ended)
    assert await store.active(rig.redis, 1, -100) == fresh
    assert await store.pending(rig.redis, 1) == [fresh]


async def test_bot_chat_and_namespace_are_isolated(rig):
    values = [saved(), saved(bot=2), saved(chat=-200)]
    for value in values:
        await store.save(rig.redis, value)
    for value in values:
        game = value.game
        assert await store.active(rig.redis, game.bot_id, game.chat_id) == value
    assert len(await store.pending(rig.redis, 1)) == 2
    assert len(await store.pending(rig.redis, 2)) == 1
    other = RedisStorage(rig.client, prefix=rig.namespace + ":other", supervisor=Supervisor())
    assert await store.active(other, 1, -100) is None


async def test_recovery_preserves_clocks_history_and_player_names(rig):
    value = saved()
    value.game.join(Player(user_id=22, name="Black", username="black"), time())
    value.game.move(21, "e2e4", time() + 10)
    await store.save(rig.redis, value)
    restored = (await store.pending(rig.redis, 1))[0]
    assert restored == value
    assert restored.game.white_seconds == pytest.approx(595, abs=0.01)
    assert restored.game.moves == ["e2e4"]
    assert restored.game.deadline() == value.game.deadline()


async def test_snapshots_expire_but_reads_do_not_extend_lifetime(rig):
    value = saved()
    await store.save(rig.redis, value)
    key = store._record(rig.redis, 1, -100, value.game.token)
    expiry = await rig.client.expiretime(key)
    assert 86398 <= await rig.client.ttl(key) <= 86400
    await store.load(rig.redis, 1, -100, value.game.token)
    await store.pending(rig.redis, 1)
    assert await rig.client.expiretime(key) == expiry


async def test_corrupt_or_missing_record_does_not_block_other_recovery(rig):
    good = saved()
    broken = saved(chat=-200)
    await store.save(rig.redis, good)
    await store.save(rig.redis, broken)
    key = store._record(rig.redis, 1, -200, broken.game.token)
    await rig.client.set(key, "bad JSON", ex=60)
    assert await store.pending(rig.redis, 1) == [good]
    assert await rig.client.zscore(store._pending(rig.redis, 1), key) is None


async def test_wrong_identity_cannot_be_loaded(rig):
    value = saved()
    await store.save(rig.redis, value)
    key = store._record(rig.redis, 1, -100, value.game.token)
    value.game.bot_id = 2
    await rig.client.set(key, value.model_dump_json(), ex=60)
    with pytest.raises(ValueError, match="identity"):
        await store.load(rig.redis, 1, -100, value.game.token)
