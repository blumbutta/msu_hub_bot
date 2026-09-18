"""Global Elo contracts, including opt-in tests against the real Redis Lua."""

import asyncio
import os
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from msu_hub_bot.commands.chess_play_game import Game, Player
from msu_hub_bot.telegram import chess_play_rating as rating
from msu_hub_bot.telegram.runtime import Supervisor
from msu_hub_bot.telegram.storage import RedisStorage

pytestmark = pytest.mark.allow_hosts(["127.0.0.1", "::1", "localhost"])
WHITE = Player(user_id=2, name="Белый <игрок>", username="white")
BLACK = Player(user_id=10, name="Чёрный", username="black")


def finished(*, token="game", chat_id=-100, bot_id=123, winner=WHITE.user_id, snapshots=(800, 800), result="resigned"):
    game = Game(token=token, bot_id=bot_id, chat_id=chat_id, white=WHITE, created_at=1000, invite_deadline=1600)
    game.join(BLACK, 1100)
    game.white_rating, game.black_rating = snapshots
    game.result, game.winner, game.turn_started = result, winner, None
    return game


@pytest.mark.parametrize(
    "white,black,score,delta",
    [
        (800, 800, 1, 16),
        (800, 800, 0, -16),
        (800, 800, 0.5, 0),
        (1200, 800, 1, 3),
        (1200, 800, 0, -29),
        (1200, 800, 0.5, -13),
        (800, 1200, 1, 29),
        (800, 1200, 0.5, 13),
        (10**400, 0, 0, -32),
        (0, 10**400, 1, 32),
    ],
)
def test_elo_rewards_upsets_more_and_is_zero_sum(white, black, score, delta):
    assert rating.elo_delta(white, black, score) == delta
    assert rating.elo_delta(black, white, 1 - score) == -delta


@pytest.mark.parametrize("score", [-1, 0.1, 2, float("nan"), float("inf")])
def test_invalid_elo_result_is_rejected(score):
    with pytest.raises(ValueError, match="Elo score"):
        rating.elo_delta(800, 800, score)


@pytest.fixture
async def records():
    url = os.environ.get("HUB_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set HUB_TEST_REDIS_URL for real chess-play rating contracts")
    target = urlsplit(url)
    if target.scheme != "redis" or target.hostname not in {"127.0.0.1", "::1", "localhost"}:
        pytest.fail("Chess-play Redis contracts require a loopback Redis endpoint")
    client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
    namespace = f"hub_test_chess_rating:{uuid4().hex}"
    storage = RedisStorage(client, prefix=namespace, supervisor=Supervisor())
    try:
        await client.ping()
        yield SimpleNamespace(client=client, storage=storage, namespace=namespace)
    finally:
        try:
            keys = [key async for key in client.scan_iter(match=namespace + ":*")]
            if keys:
                # Only this test's UUID-owned records can be removed.
                await client.delete(*keys)
        finally:
            await client.aclose()


async def test_new_user_is_unranked_at_800_until_joining_game(records):
    player = await rating.get_player(123, WHITE.user_id, records.storage)
    assert player.rating == 800
    assert player.rank is None
    page = await rating.leaderboard(123, records.storage)
    assert page.players == ()
    assert (page.page, page.pages, page.total) == (0, 1, 0)
    assert await rating.start_pair(123, WHITE, BLACK, records.storage) == (800, 800)
    page = await rating.leaderboard(123, records.storage)
    assert [(row.user_id, row.rank, row.rating) for row in page.players] == [(2, 1, 800), (10, 2, 800)]
    assert page.players[0].name == WHITE.name
    assert page.players[0].username == WHITE.username


async def test_atomic_replayed_settlement_is_permanent_and_zero_sum(records):
    game = finished()
    await rating.start_pair(game.bot_id, WHITE, BLACK, records.storage)
    results = await asyncio.gather(*(rating.settle(game, records.storage) for _ in range(24)))
    assert all(value == ((800, 816), (800, 784)) for value in results)
    assert (await rating.get_player(123, 2, records.storage)).rating == 816
    assert (await rating.get_player(123, 10, records.storage)).rating == 784
    keys = [key async for key in records.client.scan_iter(match=records.namespace + ":*")]
    assert len(keys) == 4
    assert await asyncio.gather(*(records.client.ttl(key) for key in keys)) == [-1] * len(keys)


async def test_lost_response_after_commit_can_be_retried_without_scoring_again(records, monkeypatch):
    game = finished()
    original_eval = records.client.eval

    async def lose_response(*args, **kwargs):
        await original_eval(*args, **kwargs)
        raise TimeoutError("Synthetic lost settlement response")

    with monkeypatch.context() as lost:
        lost.setattr(records.client, "eval", lose_response)
        with pytest.raises(TimeoutError, match="Synthetic lost"):
            await rating.settle(game, records.storage)
    assert await rating.settle(game, records.storage) == ((800, 816), (800, 784))
    assert (await rating.get_player(123, WHITE.user_id, records.storage)).rating == 816


async def test_simultaneous_games_in_other_chats_apply_snapshot_delta_to_current_rating(records):
    first = finished(chat_id=-100)
    second = finished(chat_id=-200)
    assert await rating.start_pair(123, WHITE, BLACK, records.storage) == (800, 800)
    results = await asyncio.gather(rating.settle(first, records.storage), rating.settle(second, records.storage))
    assert sorted(results) == [((800, 816), (800, 784)), ((816, 832), (784, 768))]
    assert await rating.start_pair(123, WHITE, BLACK, records.storage) == (832, 768)
    assert (await rating.get_player(123, WHITE.user_id, records.storage)).rating == 832
    assert (await rating.get_player(123, BLACK.user_id, records.storage)).rating == 768


async def test_elo_expectation_uses_join_snapshot_not_current_rating(records):
    game = finished(snapshots=(1200, 800))
    assert await rating.settle(game, records.storage) == ((800, 803), (800, 797))
    drawn = finished(token="draw", snapshots=(1200, 800), winner=None, result="agreed_draw")
    assert await rating.settle(drawn, records.storage) == ((803, 790), (797, 810))
    black_win = finished(token="timeout", winner=BLACK.user_id, result="timeout")
    assert await rating.settle(black_win, records.storage) == ((790, 774), (810, 826))


async def test_bot_namespace_and_round_idempotency_keys_are_independent(records):
    for game in (finished(), finished(token="other"), finished(bot_id=456)):
        await rating.settle(game, records.storage)
    assert (await rating.get_player(123, WHITE.user_id, records.storage)).rating == 832
    assert (await rating.get_player(456, WHITE.user_id, records.storage)).rating == 816


async def test_names_refresh_without_resetting_rating_or_registering_spectators(records):
    await rating.settle(finished(), records.storage)
    renamed = Player(user_id=WHITE.user_id, name="Новое имя", username=None)
    assert await rating.start_pair(123, renamed, BLACK, records.storage) == (816, 784)
    player = await rating.get_player(123, renamed.user_id, records.storage)
    assert (player.name, player.username, player.rating) == ("Новое имя", None, 816)
    await rating.get_player(123, 999, records.storage)
    assert (await rating.leaderboard(123, records.storage)).total == 2


async def test_leaderboard_pages_every_player_with_numeric_tie_order_and_no_duplicates(records):
    for user_id in range(3, 28):
        await rating.start_pair(123, WHITE, Player(user_id=user_id, name=f"Игрок {user_id}"), records.storage)
    first = await rating.leaderboard(123, records.storage, page=0, page_size=10)
    second = await rating.leaderboard(123, records.storage, page=1, page_size=10)
    third = await rating.leaderboard(123, records.storage, page=2, page_size=10)
    assert (first.total, first.pages) == (26, 3)
    rows = first.players + second.players + third.players
    assert [row.user_id for row in rows] == list(range(2, 28))
    assert [row.rank for row in rows] == list(range(1, 27))
    assert (await rating.leaderboard(123, records.storage, page=999)).page == 2
    assert (await rating.leaderboard(123, records.storage, page=-1)).page == 0


async def test_large_telegram_ids_are_returned_without_lua_json_precision_loss(records):
    white = Player(user_id=4503599627370494, name="Большой ID")
    black = Player(user_id=4503599627370495, name="Другой ID")
    await rating.start_pair(123, white, black, records.storage)
    assert (await rating.get_player(123, white.user_id, records.storage)).user_id == white.user_id
    rows = (await rating.leaderboard(123, records.storage)).players
    assert [row.user_id for row in rows] == [white.user_id, black.user_id]


async def test_unplayed_or_invalid_results_cannot_change_ratings(records):
    for game in (finished(result="cancelled"), finished(result="invite_expired"), finished(winner=999)):
        with pytest.raises(ValueError):
            await rating.settle(game, records.storage)
    with pytest.raises(ValueError):
        await rating.start_pair(123, WHITE, WHITE, records.storage)
    assert (await rating.leaderboard(123, records.storage)).total == 0
