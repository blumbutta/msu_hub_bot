"""Permanent, bot-wide Elo with atomic and replay-safe game settlement."""

import asyncio
import math
from collections.abc import Awaitable
from typing import cast

from pydantic import BaseModel, TypeAdapter

from msu_hub_bot.commands.chess_play_game import Game, Player
from msu_hub_bot.telegram.storage import RedisStorage

INITIAL_RATING = 800
K_FACTOR = 32
STORE_TIMEOUT = 5
RatingChange = tuple[tuple[int, int], tuple[int, int]]
_PAIR = TypeAdapter(tuple[int, int])
_CHANGE: TypeAdapter[RatingChange] = TypeAdapter(RatingChange)


class RatedPlayer(BaseModel):
    user_id: int
    name: str
    username: str | None = None
    rating: int = INITIAL_RATING
    rank: int | None = None


class RatingPage(BaseModel):
    players: tuple[RatedPlayer, ...]
    total: int
    page: int
    pages: int


def elo_delta(white_rating: int, black_rating: int, white_score: float) -> int:
    if white_score not in (0.0, 0.5, 1.0):
        raise ValueError("Elo score must be 0, 0.5 or 1")
    # Clamping avoids floating-point overflow while retaining integer precision.
    difference = min(6400, max(-6400, black_rating - white_rating))
    expected = 1.0 / (1.0 + math.pow(10.0, difference / 400.0))
    delta = K_FACTOR * (white_score - expected)
    return math.floor(delta + 0.5) if delta >= 0 else -math.floor(-delta + 0.5)


def _member(user_id: int) -> str:
    if not 0 < user_id < 10**20:
        raise ValueError("Invalid Telegram user id")
    return f"{user_id:020d}"


def _keys(bot_id: int, redis: RedisStorage) -> tuple[str, str, str]:
    key = redis.generate_key("chess_play_rating", bot_id)
    return key, key + ":names", key + ":usernames"


_START_PAIR = """
for i = 1, 6, 3 do
    redis.call('ZADD', KEYS[1], 'NX', -800, ARGV[i])
    redis.call('HSET', KEYS[2], ARGV[i], ARGV[i + 1])
    redis.call('HSET', KEYS[3], ARGV[i], ARGV[i + 2])
end
return cjson.encode({-tonumber(redis.call('ZSCORE', KEYS[1], ARGV[1])),
                     -tonumber(redis.call('ZSCORE', KEYS[1], ARGV[4]))})
"""


async def start_pair(bot_id: int, white: Player, black: Player, redis: RedisStorage) -> tuple[int, int]:
    """Register players and capture both starting ratings in one operation."""
    if white.user_id == black.user_id:
        raise ValueError("A player cannot play against themselves")
    async with asyncio.timeout(STORE_TIMEOUT):
        client = await redis.redis()
        data = await cast(
            Awaitable[str],
            client.eval(
                _START_PAIR,
                3,
                *_keys(bot_id, redis),
                _member(white.user_id),
                white.name,
                white.username or "",
                _member(black.user_id),
                black.name,
                black.username or "",
            ),
        )
    return _PAIR.validate_json(data)


_SETTLE = """
local previous = redis.call('GET', KEYS[4])
if previous then
    return previous
end
local white_before = -tonumber(redis.call('ZSCORE', KEYS[1], ARGV[1]) or '-800')
local black_before = -tonumber(redis.call('ZSCORE', KEYS[1], ARGV[2]) or '-800')
local delta = tonumber(ARGV[3])
local white_after, black_after = white_before + delta, black_before - delta
redis.call('ZADD', KEYS[1], -white_after, ARGV[1], -black_after, ARGV[2])
redis.call('HSET', KEYS[2], ARGV[1], ARGV[4], ARGV[2], ARGV[6])
redis.call('HSET', KEYS[3], ARGV[1], ARGV[5], ARGV[2], ARGV[7])
local result = cjson.encode({{white_before, white_after}, {black_before, black_after}})
redis.call('SET', KEYS[4], result)
return result
"""


async def settle(game: Game, redis: RedisStorage) -> RatingChange:
    """Apply the snapshot-based Elo delta once, even after a lost response."""
    black = game.black
    if black is None or game.result not in {
        "checkmate",
        "stalemate",
        "insufficient_material",
        "seventyfive_moves",
        "fivefold_repetition",
        "fifty_moves",
        "threefold_repetition",
        "timeout",
        "resigned",
        "agreed_draw",
    }:
        raise ValueError("Only a completed game with two players can affect Elo")
    if game.white.user_id == black.user_id or game.winner not in (None, game.white.user_id, black.user_id):
        raise ValueError("Invalid game participants or winner")
    score = 0.5 if game.winner is None else float(game.winner == game.white.user_id)
    delta = elo_delta(game.white_rating, game.black_rating, score)
    marker = redis.generate_key("chess_play_rating", game.bot_id, "games", game.chat_id, game.token)
    async with asyncio.timeout(STORE_TIMEOUT):
        client = await redis.redis()
        data = await cast(
            Awaitable[str],
            client.eval(
                _SETTLE,
                4,
                *_keys(game.bot_id, redis),
                marker,
                _member(game.white.user_id),
                _member(black.user_id),
                delta,
                game.white.name,
                game.white.username or "",
                black.name,
                black.username or "",
            ),
        )
    return _CHANGE.validate_json(data)


_PLAYER = """
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
local rank = redis.call('ZRANK', KEYS[1], ARGV[1])
local username = redis.call('HGET', KEYS[3], ARGV[1])
return cjson.encode({user_id=ARGV[1],
    name=redis.call('HGET', KEYS[2], ARGV[1]) or 'Игрок',
    username=(username and username ~= '' and username) or cjson.null,
    rating=score and -tonumber(score) or 800,
    rank=rank and rank + 1 or cjson.null})
"""


async def get_player(bot_id: int, user_id: int, redis: RedisStorage) -> RatedPlayer:
    async with asyncio.timeout(STORE_TIMEOUT):
        client = await redis.redis()
        data = await cast(Awaitable[str], client.eval(_PLAYER, 3, *_keys(bot_id, redis), _member(user_id)))
    return RatedPlayer.model_validate_json(data)


_LEADERBOARD = """
local total = redis.call('ZCARD', KEYS[1])
local size = tonumber(ARGV[2])
local pages = math.max(1, math.ceil(total / size))
local page = math.max(0, math.min(pages - 1, tonumber(ARGV[1])))
local start = page * size
local entries = redis.call('ZRANGE', KEYS[1], start, start + size - 1, 'WITHSCORES')
local rows = {}
for i = 1, #entries, 2 do
    local member, score = entries[i], entries[i + 1]
    local username = redis.call('HGET', KEYS[3], member)
    rows[#rows + 1] = {user_id=member,
        name=redis.call('HGET', KEYS[2], member) or 'Игрок',
        username=(username and username ~= '' and username) or cjson.null,
        rating=-tonumber(score), rank=start + #rows + 1}
end
-- Redis's Lua cjson encodes an empty table as {}, not [].
local players = #rows == 0 and '[]' or cjson.encode(rows)
return '{"players":' .. players .. ',"total":' .. total .. ',"page":' .. page .. ',"pages":' .. pages .. '}'
"""


async def leaderboard(bot_id: int, redis: RedisStorage, *, page: int = 0, page_size: int = 10) -> RatingPage:
    if not 1 <= page_size <= 100:
        raise ValueError("Leaderboard page size must be between 1 and 100")
    async with asyncio.timeout(STORE_TIMEOUT):
        client = await redis.redis()
        data = await cast(Awaitable[str], client.eval(_LEADERBOARD, 3, *_keys(bot_id, redis), page, page_size))
    return RatingPage.model_validate_json(data)
