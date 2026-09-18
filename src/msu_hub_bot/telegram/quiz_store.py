"""Short-lived quiz snapshots for restart recovery and completed callbacks."""

import asyncio
import math
from collections.abc import Awaitable
from datetime import date
from time import time
from typing import cast

from aiogram import Bot
from aiogram.types import Message
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from msu_hub_bot.telegram.storage import RedisStorage

TIMEOUT = 5
RETENTION = 24 * 60 * 60
SCAN_COUNT = 100


class SavedRound(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    message: Message
    question: str
    options: list[str] = Field(default_factory=list)
    votes: dict[int, tuple[int, str]] = Field(default_factory=dict)
    usernames: dict[int, str | None] = Field(default_factory=dict)
    deadline: float = Field(gt=0, allow_inf_nan=False)
    closed: bool = False
    score_day: date | None = None
    scored: bool | None = None
    revealed: bool = False


def _key(kind: str, bot_id: int, chat_id: int, token: str, redis: RedisStorage) -> str:
    return redis.generate_key("quiz", bot_id, kind, chat_id, token)


def _index(kind: str, bot_id: int, redis: RedisStorage) -> str:
    return redis.generate_key("quiz", bot_id, kind, "pending")


def _needs_recovery(state: SavedRound) -> bool:
    return not state.closed or not state.revealed or state.scored is not True


def _parse(raw: str | bytes | None, kind: str, bot: Bot, key: str, redis: RedisStorage) -> SavedRound | None:
    if raw is None:
        return None
    try:
        state = SavedRound.model_validate_json(raw, context={"bot": bot})
    except ValidationError:
        return None
    if state.deadline + RETENTION <= time() or _key(kind, bot.id, state.message.chat.id, state.token, redis) != key:
        return None
    state.message.as_(bot)
    return state


async def save(kind: str, state: SavedRound, redis: RedisStorage) -> None:
    """Atomically persist the snapshot and whether it needs background recovery."""
    async with asyncio.timeout(TIMEOUT):
        bot = state.message.bot
        if bot is None:
            raise ValueError("A quiz snapshot requires a mounted bot message")
        key = _key(kind, bot.id, state.message.chat.id, state.token, redis)
        index = _index(kind, bot.id, redis)
        expires = math.ceil(state.deadline + RETENTION)
        client = await redis.redis()
        async with client.pipeline(transaction=True) as pipe:
            # A fixed expiry cannot be extended by callback navigation or recovery.
            pipe.set(key, state.model_dump_json(), exat=expires)
            if _needs_recovery(state) and expires > time():
                pipe.zadd(index, {key: expires})
            else:
                pipe.zrem(index, key)
            await pipe.execute()


async def load(kind: str, bot: Bot, chat_id: int, token: str, redis: RedisStorage) -> SavedRound | None:
    """Restore one exact round, including completed rounds reached by callbacks."""
    async with asyncio.timeout(TIMEOUT):
        client = await redis.redis()
        key = _key(kind, bot.id, chat_id, token, redis)
        raw = await client.get(key)
        state = _parse(raw, kind, bot, key, redis)
        if state is None:
            await cast(Awaitable[int], client.zrem(_index(kind, bot.id, redis), key))
        return state


async def pending(kind: str, bot: Bot, redis: RedisStorage) -> list[SavedRound]:
    """Recover unfinished work in batches; stale index records are discarded."""
    async with asyncio.timeout(TIMEOUT):
        client = await redis.redis()
        index = _index(kind, bot.id, redis)
        prefix = redis.generate_key("quiz", bot.id, kind) + ":"
        cursor = 0
        seen: set[str] = set()
        states: list[SavedRound] = []
        while True:
            cursor, entries = await cast(
                Awaitable[tuple[int, list[tuple[str, float]]]], client.zscan(index, cursor=cursor, count=SCAN_COUNT)
            )
            keys = [key for key, _ in entries if key not in seen]
            seen.update(keys)
            # Only snapshots from this bot and quiz namespace may be dereferenced.
            valid = [key for key in keys if key.startswith(prefix) and key != index]
            stale = [key for key in keys if key not in valid]
            raws = await client.mget(valid) if valid else []
            for key, raw in zip(valid, raws, strict=True):
                state = _parse(raw, kind, bot, key, redis)
                if state is None or not _needs_recovery(state):
                    stale.append(key)
                else:
                    states.append(state)
            if stale:
                await cast(Awaitable[int], client.zrem(index, *stale))
            if cursor == 0:
                return states
