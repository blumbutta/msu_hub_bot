import asyncio
import json
import math
import os
from datetime import date
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from aiogram import Bot
from redis.asyncio import Redis

from msu_hub_bot.telegram import quiz_store
from msu_hub_bot.telegram.quiz_store import SavedRound
from msu_hub_bot.telegram.runtime import Supervisor
from msu_hub_bot.telegram.storage import RedisStorage
from telegram_helpers import RecordingSession, make_bot, make_message


class MemoryPipeline:
    def __init__(self, client):
        self.client = client
        self.commands = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def set(self, key, value, *, exat):
        self.commands.append(("set", key, value, exat))

    def zadd(self, key, mapping):
        self.commands.append(("zadd", key, mapping))

    def zrem(self, key, *members):
        self.commands.append(("zrem", key, members))

    async def execute(self):
        if self.client.block == "save":
            await asyncio.Event().wait()
        for command, key, *args in self.commands:
            if command == "set":
                value, expiry = args
                self.client.values[key] = value
                self.client.expiries[key] = expiry
            elif command == "zadd":
                self.client.indexes.setdefault(key, {}).update(args[0])
            else:
                for member in args[0]:
                    self.client.indexes.setdefault(key, {}).pop(member, None)
        self.client.transactions.append(self.commands)


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.expiries = {}
        self.indexes = {}
        self.transactions = []
        self.reads = []
        self.scan_calls = 0
        self.scan_snapshot = []
        self.block = None

    def pipeline(self, *, transaction):
        assert transaction is True
        return MemoryPipeline(self)

    async def get(self, key):
        if self.block == "load":
            await asyncio.Event().wait()
        self.reads.append(key)
        if self.expiries.get(key, float("inf")) <= quiz_store.time():
            return None
        return self.values.get(key)

    async def mget(self, keys):
        return [await self.get(key) for key in keys]

    async def zrem(self, key, *members):
        for member in members:
            self.indexes.setdefault(key, {}).pop(member, None)

    async def zscan(self, key, *, cursor, count):
        if self.block == "pending":
            await asyncio.Event().wait()
        self.scan_calls += 1
        if cursor == 0:
            self.scan_snapshot = list(self.indexes.get(key, {}).items())
        batch = self.scan_snapshot[cursor : cursor + count]
        end = cursor + count
        return (end if end < len(self.scan_snapshot) else 0), batch


@pytest.fixture
async def rig(monkeypatch):
    monkeypatch.setattr(quiz_store, "time", lambda: 1_800_000_000)
    client = MemoryRedis()
    bot = make_bot()
    replacement = make_bot()
    redis = RedisStorage(client, prefix="quiz-test", supervisor=Supervisor())
    state = SavedRound(
        token="round-one",
        message=make_message(bot, message_id=200, message_thread_id=17, is_topic_message=True),
        question='{"country":"Норвегия"}',
        options=["Норвегия", "Россия"],
        votes={41: (0, "Имя 🧭"), 42: (1, "Другой <name>")},
        usernames={41: "first", 42: None},
        deadline=quiz_store.time() + 600,
        score_day=date(2027, 1, 15),
    )
    try:
        yield SimpleNamespace(client=client, redis=redis, bot=bot, replacement=replacement, state=state)
    finally:
        await bot.session.close()
        await replacement.session.close()


def snapshot_key(rig, kind="geoguess", state=None):
    state = state or rig.state
    return rig.redis.generate_key("quiz", rig.bot.id, kind, state.message.chat.id, state.token)


def index_key(rig, kind="geoguess"):
    return rig.redis.generate_key("quiz", rig.bot.id, kind, "pending")


async def test_snapshot_restores_round_and_mounts_the_current_bot_without_serializing_credentials(rig):
    await quiz_store.save("geoguess", rig.state, rig.redis)
    key = snapshot_key(rig)
    raw = rig.client.values[key]
    assert rig.bot.token not in raw and '"_bot"' not in raw
    loaded = await quiz_store.load("geoguess", rig.replacement, rig.state.message.chat.id, rig.state.token, rig.redis)
    assert loaded is not None and loaded is not rig.state
    assert loaded.message.bot is rig.replacement
    assert loaded.message.reply("test").bot is rig.replacement
    assert loaded.message.message_thread_id == 17
    assert loaded.message.message_id == 200
    assert loaded.model_dump() == rig.state.model_dump()
    assert loaded.votes[41] == (0, "Имя 🧭")
    assert loaded.usernames[42] is None
    assert rig.client.expiries[key] == rig.state.deadline + quiz_store.RETENTION
    assert [command[0] for command in rig.client.transactions[0]] == ["set", "zadd"]


async def test_completed_rounds_remain_loadable_but_expiry_is_never_extended(rig, monkeypatch):
    await quiz_store.save("geoguess", rig.state, rig.redis)
    key = snapshot_key(rig)
    expiry = rig.client.expiries[key]
    completed = rig.state.model_copy(update={"closed": True, "scored": True, "revealed": True})
    monkeypatch.setattr(quiz_store, "time", lambda: 1_800_000_300)
    await quiz_store.save("geoguess", completed, rig.redis)
    assert rig.client.expiries[key] == expiry
    assert key not in rig.client.indexes[index_key(rig)]
    assert await quiz_store.pending("geoguess", rig.bot, rig.redis) == []
    loaded = await quiz_store.load("geoguess", rig.bot, completed.message.chat.id, completed.token, rig.redis)
    assert loaded is not None and loaded.closed and loaded.revealed and loaded.scored
    assert [command[0] for command in rig.client.transactions[-1]] == ["set", "zrem"]


@pytest.mark.parametrize(
    "changes",
    [{}, {"closed": True}, {"closed": True, "revealed": True, "scored": False}, {"closed": True, "scored": True}],
)
async def test_pending_includes_active_unscored_and_unrevealed_rounds(rig, changes):
    state = rig.state.model_copy(update=changes)
    await quiz_store.save("chess", state, rig.redis)
    pending = await quiz_store.pending("chess", rig.replacement, rig.redis)
    assert len(pending) == 1 and pending[0].model_dump() == state.model_dump()
    assert pending[0].message.bot is rig.replacement


async def test_kind_bot_chat_and_round_namespaces_are_independent(rig):
    await quiz_store.save("geoguess", rig.state, rig.redis)
    chat_id, token = rig.state.message.chat.id, rig.state.token
    other_bot = Bot("987654321:" + "b" * 35, session=RecordingSession())
    try:
        assert await quiz_store.load("chess", rig.bot, chat_id, token, rig.redis) is None
        assert await quiz_store.load("geoguess", other_bot, chat_id, token, rig.redis) is None
        assert await quiz_store.load("geoguess", rig.bot, chat_id + 1, token, rig.redis) is None
        assert await quiz_store.load("geoguess", rig.bot, chat_id, "another-round", rig.redis) is None
        assert await quiz_store.pending("chess", rig.bot, rig.redis) == []
        assert await quiz_store.pending("geoguess", other_bot, rig.redis) == []
        assert len(await quiz_store.pending("geoguess", rig.bot, rig.redis)) == 1
    finally:
        await other_bot.session.close()


async def test_missing_malformed_mismatched_and_expired_snapshots_are_removed_from_index(rig):
    index = index_key(rig)
    prefix = rig.redis.generate_key("quiz", rig.bot.id, "geoguess", rig.state.message.chat.id)
    good = rig.state.model_copy(update={"token": "good"})
    await quiz_store.save("geoguess", good, rig.redis)
    entries = {
        f"{prefix}:missing": None,
        f"{prefix}:malformed": "{broken json",
        f"{prefix}:wrong-token": good.model_dump_json(),
        f"{prefix}:expired": rig.state.model_copy(
            update={"token": "expired", "deadline": quiz_store.time() - quiz_store.RETENTION}
        ).model_dump_json(),
        "unrelated:private:key": "must not be read",
        index: "not a snapshot",
    }
    for key, value in entries.items():
        if value is not None:
            rig.client.values[key] = value
        rig.client.indexes[index][key] = quiz_store.time() + quiz_store.RETENTION
    pending = await quiz_store.pending("geoguess", rig.bot, rig.redis)
    assert [state.token for state in pending] == ["good"]
    assert set(rig.client.indexes[index]) == {snapshot_key(rig, state=good)}
    assert "unrelated:private:key" not in rig.client.reads and index not in rig.client.reads


async def test_load_rejects_invalid_metadata_and_expired_state(rig, monkeypatch):
    await quiz_store.save("geoguess", rig.state, rig.redis)
    key = snapshot_key(rig)
    raw = json.loads(rig.client.values[key])
    raw["deadline"] = "NaN"
    rig.client.values[key] = json.dumps(raw)
    assert await quiz_store.load("geoguess", rig.bot, rig.state.message.chat.id, rig.state.token, rig.redis) is None
    assert key not in rig.client.indexes[index_key(rig)]
    await quiz_store.save("geoguess", rig.state, rig.redis)
    monkeypatch.setattr(quiz_store, "time", lambda: rig.state.deadline + quiz_store.RETENTION)
    assert await quiz_store.load("geoguess", rig.bot, rig.state.message.chat.id, rig.state.token, rig.redis) is None
    assert key not in rig.client.indexes[index_key(rig)]


async def test_pending_scans_multiple_batches_and_deduplicates_returned_entries(rig, monkeypatch):
    monkeypatch.setattr(quiz_store, "SCAN_COUNT", 3)
    for index in range(11):
        await quiz_store.save("geoguess", rig.state.model_copy(update={"token": f"round-{index}"}), rig.redis)
    scan = rig.client.zscan

    async def duplicate_boundary(*args, **kwargs):
        cursor, entries = await scan(*args, **kwargs)
        if kwargs["cursor"]:
            entries.insert(0, rig.client.scan_snapshot[0])
        return cursor, entries

    monkeypatch.setattr(rig.client, "zscan", duplicate_boundary)
    recovered = await quiz_store.pending("geoguess", rig.bot, rig.redis)
    assert {state.token for state in recovered} == {f"round-{index}" for index in range(11)}
    assert len(recovered) == 11 and rig.client.scan_calls == 4


@pytest.mark.parametrize("operation", ["save", "load", "pending"])
async def test_each_operation_has_a_whole_request_deadline(rig, monkeypatch, operation):
    monkeypatch.setattr(quiz_store, "TIMEOUT", 0.01)
    rig.client.block = operation
    with pytest.raises(TimeoutError):
        if operation == "save":
            await quiz_store.save("geoguess", rig.state, rig.redis)
        elif operation == "load":
            await quiz_store.load("geoguess", rig.bot, rig.state.message.chat.id, rig.state.token, rig.redis)
        else:
            await quiz_store.pending("geoguess", rig.bot, rig.redis)
    assert not rig.client.transactions


async def test_unmounted_message_cannot_be_persisted_under_an_unknown_bot(rig):
    state = rig.state.model_copy(update={"message": make_message()})
    with pytest.raises(ValueError, match="mounted bot"):
        await quiz_store.save("geoguess", state, rig.redis)
    assert not rig.client.transactions


@pytest.mark.allow_hosts(["127.0.0.1", "::1", "localhost"])
@pytest.mark.parametrize("kind", ["geoguess", "chess"])
async def test_real_redis_recovers_pending_work_and_retains_completed_callbacks(kind):
    url = os.environ.get("HUB_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set HUB_TEST_REDIS_URL for real quiz snapshot contracts")
    target = urlsplit(url)
    if target.scheme != "redis" or target.hostname not in {"127.0.0.1", "::1", "localhost"}:
        pytest.fail("Quiz snapshot contracts require a loopback Redis endpoint")
    client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
    redis = RedisStorage(client, prefix=f"hub_test_quiz_store:{uuid4().hex}", supervisor=Supervisor())
    bot, replacement = make_bot(), make_bot()
    state = SavedRound(
        token="active",
        message=make_message(bot, message_id=201, message_thread_id=17, is_topic_message=True),
        question='{"synthetic":"question"}',
        votes={42: (1, "Игрок")},
        usernames={42: "synthetic"},
        deadline=quiz_store.time() + 600,
    )
    index = redis.generate_key("quiz", bot.id, kind, "pending")
    keys = {index}

    def key(token):
        value = redis.generate_key("quiz", bot.id, kind, state.message.chat.id, token)
        keys.add(value)
        return value

    try:
        expiry = math.ceil(state.deadline + quiz_store.RETENTION)
        states = [
            state,
            state.model_copy(update={"token": "unrevealed", "closed": True, "scored": True}),
            state.model_copy(update={"token": "unscored", "closed": True, "revealed": True, "scored": False}),
            state.model_copy(update={"token": "completed", "closed": True, "revealed": True, "scored": True}),
        ]
        for item in states:
            key(item.token)
            await quiz_store.save(kind, item, redis)
        raw = await client.get(key("active"))
        assert raw is not None and bot.token not in raw
        assert await client.expiretime(key("active")) == expiry
        recovered = await quiz_store.pending(kind, replacement, redis)
        assert {item.token for item in recovered} == {"active", "unrevealed", "unscored"}
        assert all(item.message.bot is replacement for item in recovered)
        assert all(item.message.message_thread_id == 17 and item.votes == state.votes for item in recovered)
        completed = await quiz_store.load(kind, replacement, state.message.chat.id, "completed", redis)
        assert completed is not None and completed.revealed and completed.scored and completed.closed
        await quiz_store.save(kind, state.model_copy(update={"closed": True, "revealed": True, "scored": True}), redis)
        assert await client.expiretime(key("active")) == expiry
        assert await client.zscore(index, key("active")) is None
        await client.set(key("malformed"), "{invalid JSON", ex=60)
        await client.zadd(index, {key("missing"): expiry, key("malformed"): expiry, key("completed"): expiry})
        recovered = await quiz_store.pending(kind, replacement, redis)
        assert {item.token for item in recovered} == {"unrevealed", "unscored"}
        assert await client.zcard(index) == 2
        assert await quiz_store.load(kind, replacement, state.message.chat.id, "missing", redis) is None
    finally:
        try:
            # Remove only records belonging to this test's freshly generated UUID.
            await client.delete(*keys)
        finally:
            await client.aclose()
            await bot.session.close()
            await replacement.session.close()
