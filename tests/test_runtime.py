"""Exercise the real composition root with offline provider/Telegram boundaries."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.enums import UpdateType
from aiogram.methods import DeleteWebhook, GetMe

from msu_hub_bot.settings import Settings
from telegram_helpers import RecordingSession


@pytest.fixture
def app_settings():
    return Settings(
        bot_token="123456789:" + "a" * 35,
        redis_host="localhost",
        supabase_url="http://supabase.invalid",
        supabase_key="synthetic-publishable-key",
        supabase_email="bot@example.invalid",
        supabase_password="synthetic-password",
    )


@pytest.fixture
def boundaries(monkeypatch):
    from msu_hub_bot import app

    session = RecordingSession()
    client = AsyncMock()
    db = AsyncMock()
    monkeypatch.setattr(app, "AiohttpSession", lambda **kwargs: session)
    monkeypatch.setattr(app, "Redis", lambda **kwargs: client)
    monkeypatch.setattr(app, "create_repository", lambda *args, **kwargs: db)
    monkeypatch.setattr(app.ChessPlay, "restore", AsyncMock())
    monkeypatch.setattr(app.ChessPlay, "close", AsyncMock())
    monkeypatch.setattr(app.ChessPlay, "open", Mock())
    return session, client, db


async def test_composition_startup_and_idempotent_shutdown(app_settings, boundaries, monkeypatch):
    from msu_hub_bot.app import Application, ChessPlay

    session, client, db = boundaries
    application = await Application.create(app_settings)
    assert application.redis.generate_key("bot", "to_delete") == "hub:bot:to_delete"
    assert application.fsm.storage is not application.dispatcher.storage
    assert application.fsm.storage.state_ttl is None
    assert application.fsm.storage.data_ttl is None
    await application.start()
    db.check.assert_awaited_once_with()
    assert [type(method) for method in session.methods] == [GetMe, DeleteWebhook]
    assert session.methods[-1].drop_pending_updates is False
    assert application._producer is not None
    assert application._chess_play_recovery is not None
    ChessPlay.open.assert_called_once_with()
    ChessPlay.restore.assert_awaited_once_with(application.bot, application.redis, application.supervisor)
    await application.close()
    await application.close()
    client.aclose.assert_awaited_once()
    db.close.assert_awaited_once()
    assert session.closed and application._producer.done()
    assert application._chess_play_recovery.done()
    assert ChessPlay.close.await_count == 2


async def test_partial_allocation_failure_closes_opened_clients(app_settings, boundaries, monkeypatch):
    from msu_hub_bot import app

    session, client, db = boundaries

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic allocation failure")

    monkeypatch.setattr(app, "ManyJDoodle", fail)
    with pytest.raises(RuntimeError, match="allocation"):
        await app.Application.create(app_settings)
    assert session.closed
    client.aclose.assert_awaited_once()
    db.close.assert_awaited_once()


async def test_startup_failure_runs_owned_cleanup(app_settings, boundaries):
    from msu_hub_bot.app import Application

    session, client, db = boundaries
    db.check.side_effect = RuntimeError("Synthetic DB outage")
    application = await Application.create(app_settings)
    with pytest.raises(RuntimeError, match="outage"):
        await application.run()
    assert session.closed
    client.aclose.assert_awaited_once()
    assert session.methods == []


async def test_polling_explicitly_subscribes_to_all_kinds_and_preserves_backlog(app_settings, boundaries, monkeypatch):
    from msu_hub_bot.app import Application

    session, _, _ = boundaries
    application = await Application.create(app_settings)
    start_polling = AsyncMock()
    monkeypatch.setattr(application.dispatcher, "start_polling", start_polling)
    await application.run()
    start_polling.assert_awaited_once_with(
        application.bot,
        polling_timeout=60,
        handle_as_tasks=True,
        allowed_updates=[kind.value for kind in UpdateType],
        close_bot_session=False,
    )
    subscribed = start_polling.call_args.kwargs["allowed_updates"]
    assert {"message_reaction", "message_reaction_count", "chat_member", "business_message", "poll_answer"} <= set(subscribed)
    assert next(method for method in session.methods if isinstance(method, DeleteWebhook)).drop_pending_updates is False
    assert session.closed


async def test_shutdown_drains_admitted_jobs_before_closing_dependencies(app_settings, boundaries):
    from msu_hub_bot.app import Application

    session, client, _ = boundaries
    application = await Application.create(app_settings)
    started, finish = asyncio.Event(), asyncio.Event()

    async def worker():
        application.supervisor.admit_current_update()
        started.set()
        await finish.wait()

        async def late_job():
            assert not session.closed
            client.aclose.assert_not_awaited()

        application.supervisor.create_job(late_job)

    task = asyncio.create_task(worker())
    await started.wait()
    closing = asyncio.create_task(application.close())
    await asyncio.sleep(0)
    assert not session.closed
    finish.set()
    await asyncio.gather(task, closing)
    assert session.closed


def test_health_requires_recent_successful_poll(monkeypatch, tmp_path):
    from msu_hub_bot.health import heartbeat_path, mark_poll_success, ready

    monkeypatch.setenv("HUB_POLL_HEARTBEAT", str(tmp_path / "poll"))
    monkeypatch.setattr("msu_hub_bot.health.time.monotonic", lambda: 1_000)
    assert not ready()
    mark_poll_success()
    assert ready()
    heartbeat_path().write_text("800")
    assert not ready()


async def test_chess_play_recovery_retries_without_preventing_startup(app_settings, boundaries, monkeypatch):
    from msu_hub_bot import app

    monkeypatch.setattr(app, "CHESS_PLAY_RECOVERY_SECONDS", 0.001)
    recovered = asyncio.Event()
    attempts = 0

    async def restore(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("Synthetic recovery outage")
        recovered.set()

    monkeypatch.setattr(app.ChessPlay, "restore", AsyncMock(side_effect=restore))
    application = await app.Application.create(app_settings)
    try:
        await application.start()
        await asyncio.wait_for(recovered.wait(), 1)
        assert attempts >= 2
        assert application._chess_play_recovery is not None and not application._chess_play_recovery.done()
    finally:
        await application.close()


async def test_chess_play_producers_stop_before_worker_drain(app_settings, boundaries, monkeypatch):
    from msu_hub_bot import app

    application = await app.Application.create(app_settings)
    await application.start()
    order = []

    async def close_chess():
        assert application._chess_play_recovery.done()
        assert application._producer.done()
        assert not application.bot.session.closed
        order.append("chess")

    async def drain(**kwargs):
        assert order == ["chess"]
        order.append("drain")

    monkeypatch.setattr(app.ChessPlay, "close", AsyncMock(side_effect=close_chess))
    monkeypatch.setattr(application.supervisor, "drain", AsyncMock(side_effect=drain))
    await application.close()
    assert order == ["chess", "drain"]


async def test_shutdown_cancels_pending_chess_play_recovery_before_closing_redis(app_settings, boundaries, monkeypatch):
    from msu_hub_bot import app

    session, client, _ = boundaries
    application = await app.Application.create(app_settings)
    await application.start()
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def hanging_recovery(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert not session.closed
            client.aclose.assert_not_awaited()
            stopped.set()

    monkeypatch.setattr(app.ChessPlay, "restore", AsyncMock(side_effect=hanging_recovery))
    # Start a scan without changing asyncio.sleep for unrelated services.
    application._chess_play_recovery.cancel()
    await asyncio.gather(application._chess_play_recovery, return_exceptions=True)
    application._chess_play_recovery = asyncio.create_task(application._restore_chess_play())
    await entered.wait()
    await application.close()
    assert stopped.is_set() and session.closed
