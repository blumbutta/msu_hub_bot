"""Atomic, bounded chess match snapshots, isolated by bot and chat."""

import asyncio
import logging
from collections.abc import Awaitable
from time import time
from typing import cast

from pydantic import BaseModel, ConfigDict, model_validator

from msu_hub_bot.commands.chess_play_game import Game
from msu_hub_bot.telegram.storage import RedisStorage

TIMEOUT = 5
RETENTION = 24 * 60 * 60
logger = logging.getLogger(__name__)


class SavedGame(BaseModel):
    model_config = ConfigDict(extra="forbid")
    game: Game
    revealed: bool = False
    ratings: tuple[tuple[int, int], tuple[int, int]] | None = None

    @model_validator(mode="after")
    def check_completion(self) -> SavedGame:
        if self.revealed and self.game.status != "finished":
            raise ValueError("Only a completed game can be revealed")
        if self.ratings is not None and (self.game.status != "finished" or self.game.black is None):
            raise ValueError("Only a completed two-player game has rating changes")
        return self


class Conflict(RuntimeError):
    """Another transition has already won; reload before accepting input."""


_WRITE = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[2] then return 1 end
if ARGV[1] == '' then
    if current or redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
elseif current ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[1], ARGV[2], 'EXAT', ARGV[4])
if ARGV[5] == '1' then
    if redis.call('GET', KEYS[2]) == ARGV[3] then redis.call('DEL', KEYS[2]) end
else
    redis.call('SET', KEYS[2], ARGV[3], 'EXAT', ARGV[4])
end
if ARGV[6] == '1' then
    redis.call('ZREM', KEYS[3], KEYS[1])
else
    redis.call('ZADD', KEYS[3], ARGV[4], KEYS[1])
end
return 1
"""

_CLEAR_ACTIVE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


def _active(redis: RedisStorage, bot_id: int, chat_id: int) -> str:
    return redis.generate_key("chess_play", bot_id, chat_id, "active")


def _record(redis: RedisStorage, bot_id: int, chat_id: int, token: str) -> str:
    return redis.generate_key("chess_play", bot_id, chat_id, token)


def _pending(redis: RedisStorage, bot_id: int) -> str:
    return redis.generate_key("chess_play", bot_id, "pending")


async def save(redis: RedisStorage, value: SavedGame, previous: SavedGame | None = None) -> None:
    """Compare the entire snapshot, including delivery status; retry is idempotent."""
    game = value.game
    if previous is not None and (previous.game.bot_id, previous.game.chat_id, previous.game.token) != (
        game.bot_id,
        game.chat_id,
        game.token,
    ):
        raise ValueError("Cannot move a chess snapshot to another match")
    async with asyncio.timeout(TIMEOUT):
        client = await redis.redis()
        accepted = await cast(
            Awaitable[int],
            client.eval(
                _WRITE,
                3,
                _record(redis, game.bot_id, game.chat_id, game.token),
                _active(redis, game.bot_id, game.chat_id),
                _pending(redis, game.bot_id),
                previous.model_dump_json() if previous is not None else "",
                value.model_dump_json(),
                game.token,
                int(time()) + RETENTION,
                int(game.status == "finished"),
                int(game.status == "finished" and value.revealed),
            ),
        )
        if not accepted:
            raise Conflict("Chess snapshot changed")


async def load(redis: RedisStorage, bot_id: int, chat_id: int, token: str) -> SavedGame | None:
    async with asyncio.timeout(TIMEOUT):
        client = await redis.redis()
        raw = await client.get(_record(redis, bot_id, chat_id, token))
        if not raw:
            return None
        value = SavedGame.model_validate_json(raw)
        if (value.game.bot_id, value.game.chat_id, value.game.token) != (bot_id, chat_id, token):
            raise ValueError("Chess snapshot identity mismatch")
        return value


async def active(redis: RedisStorage, bot_id: int, chat_id: int) -> SavedGame | None:
    async with asyncio.timeout(TIMEOUT):
        client = await redis.redis()
        token = await client.get(_active(redis, bot_id, chat_id))
        if not token:
            return None
        try:
            value = await load(redis, bot_id, chat_id, str(token))
        except ValueError:
            logger.warning("Discarding invalid chess active pointer")
            value = None
        if value is None or value.game.status == "finished":
            await cast(Awaitable[int], client.eval(_CLEAR_ACTIVE, 1, _active(redis, bot_id, chat_id), token))
            return None
        return value


async def pending(redis: RedisStorage, bot_id: int) -> list[SavedGame]:
    async with asyncio.timeout(TIMEOUT):
        client = await redis.redis()
        index = _pending(redis, bot_id)
        await client.zremrangebyscore(index, "-inf", time())
        cursor = 0
        result: dict[str, SavedGame] = {}
        while True:
            cursor, members = await client.zscan(index, cursor=cursor, count=100)
            keys = [key for key, _ in members]
            values = await client.mget(keys) if keys else []
            for key, raw in zip(keys, values, strict=True):
                if not raw:
                    await client.zrem(index, key)
                    continue
                try:
                    value = SavedGame.model_validate_json(raw)
                    game = value.game
                    if game.bot_id != bot_id or key != _record(redis, bot_id, game.chat_id, game.token):
                        raise ValueError("Chess pending snapshot identity mismatch")
                except ValueError:
                    # One corrupt record must not block unrelated live clocks.
                    logger.warning("Skipping invalid chess snapshot")
                    await client.zrem(index, key)
                    prefix = redis.generate_key("chess_play", bot_id) + ":"
                    if key.startswith(prefix):
                        parts = key.removeprefix(prefix).split(":")
                        if len(parts) == 2:
                            try:
                                chat_id = int(parts[0])
                            except ValueError:
                                continue
                            await cast(Awaitable[int], client.eval(_CLEAR_ACTIVE, 1, _active(redis, bot_id, chat_id), parts[1]))
                    continue
                result[key] = value
            if cursor == 0:
                return list(result.values())
