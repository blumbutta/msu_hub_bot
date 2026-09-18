"""Opt-in durable quiz boundary for offline command/lifecycle tests."""

import time
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def saved_quizzes(monkeypatch):
    from msu_hub_bot.telegram import quiz_store

    class Snapshots:
        def __init__(self):
            self.states = {}
            self.save = AsyncMock(side_effect=self.put)
            self.load = AsyncMock(side_effect=self.get)
            self.pending = AsyncMock(side_effect=self.scan)

        async def put(self, kind, state, redis):
            self.states[kind, state.message.bot.id, state.message.chat.id, state.token] = state.model_dump_json()

        async def get(self, kind, bot, chat_id, token, redis):
            data = self.states.get((kind, bot.id, chat_id, token))
            if data is None:
                return None
            state = quiz_store.SavedRound.model_validate_json(data)
            state.message.as_(bot)
            return state if state.deadline + 86400 > time.time() else None

        async def scan(self, kind, bot, redis):
            states = [
                await self.get(kind, bot, chat, token, redis) for game, bid, chat, token in self.states if game == kind and bid == bot.id
            ]
            return [state for state in states if state and (not state.closed or not state.revealed or state.scored is not True)]

    snapshots = Snapshots()
    monkeypatch.setattr(quiz_store, "save", snapshots.save)
    monkeypatch.setattr(quiz_store, "load", snapshots.load)
    monkeypatch.setattr(quiz_store, "pending", snapshots.pending)
    return snapshots
