"""Exercise the real composition root with offline provider/Telegram boundaries."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import UpdateType
from aiogram.methods import DeleteWebhook, GetMe

from msu_hub_bot.settings import Settings
from telegram_helpers import RecordingSession


@pytest.fixture
def app_settings(tmp_path):
    return Settings(
        bot_token="123456789:" + "a" * 35,
        supabase_url="http://supabase.invalid",
        supabase_key="synthetic-publishable-key",
        supabase_email="bot@example.invalid",
        supabase_password="synthetic-password",
        membership_inbox_path=tmp_path / "membership-inbox.sqlite3",
    )


@pytest.fixture
def boundaries(monkeypatch):
    from msu_hub_bot import app

    session = RecordingSession()
    db = AsyncMock()
    db.feature_request.side_effect = lambda operation, request: {"version": 1} if operation == "health" else []
    monkeypatch.setattr(app, "AiohttpSession", lambda **kwargs: session)
    monkeypatch.setattr(app, "create_repository", lambda *args, **kwargs: db)
    return session, db


async def test_composition_startup_and_idempotent_shutdown(app_settings, boundaries, monkeypatch):
    from msu_hub_bot.app import Application

    session, db = boundaries
    application = await Application.create(app_settings)
    assert application.deletions.store is application.features
    assert application.fsm.storage is not application.dispatcher.storage
    assert application.fsm.storage.records.retention is None
    await application.start()
    db.check.assert_awaited_once_with()
    assert [type(method) for method in session.methods] == [GetMe, DeleteWebhook]
    assert session.methods[-1].drop_pending_updates is False
    assert application._feature_task is not None
    assert application._membership_task is not None
    assert application.dispatcher.workflow_data["quiz"] is application.quiz
    assert application.dispatcher.workflow_data["chess_matches"] is application.chess_matches
    assert application.dispatcher.workflow_data["quests"] is application.quests
    await application.close()
    await application.close()
    db.close.assert_awaited_once()
    assert session.closed
    assert application._feature_task.done()
    assert application._membership_task.done()


async def test_partial_allocation_failure_closes_opened_clients(app_settings, boundaries, monkeypatch):
    from msu_hub_bot import app

    session, db = boundaries

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic allocation failure")

    monkeypatch.setattr(app, "ManyJDoodle", fail)
    with pytest.raises(RuntimeError, match="allocation"):
        await app.Application.create(app_settings)
    assert session.closed
    db.close.assert_awaited_once()


async def test_startup_failure_runs_owned_cleanup(app_settings, boundaries):
    from msu_hub_bot.app import Application

    session, db = boundaries
    db.check.side_effect = RuntimeError("Synthetic DB outage")
    application = await Application.create(app_settings)
    with pytest.raises(RuntimeError, match="outage"):
        await application.run()
    assert session.closed
    assert session.methods == []
    assert not app_settings.membership_inbox_path.exists()


async def test_missing_feature_schema_stops_startup_before_telegram(app_settings, boundaries):
    from msu_hub_bot.app import Application
    from msu_hub_bot.storage.features import FeatureProtocolError

    session, db = boundaries
    db.feature_request.side_effect = lambda operation, request: {"version": 0}
    application = await Application.create(app_settings)
    with pytest.raises(FeatureProtocolError):
        await application.run()
    assert session.closed and session.methods == []
    assert not app_settings.membership_inbox_path.exists()
    db.close.assert_awaited_once()


async def test_polling_excludes_inline_mode_and_preserves_backlog(app_settings, boundaries, monkeypatch):
    from msu_hub_bot.app import Application

    session, _ = boundaries
    application = await Application.create(app_settings)
    start_polling = AsyncMock()
    monkeypatch.setattr(application.dispatcher, "start_polling", start_polling)
    await application.run()
    start_polling.assert_awaited_once_with(
        application.bot,
        polling_timeout=60,
        handle_as_tasks=True,
        allowed_updates=[kind.value for kind in UpdateType if kind not in {UpdateType.INLINE_QUERY, UpdateType.CHOSEN_INLINE_RESULT}],
        close_bot_session=False,
    )
    subscribed = start_polling.call_args.kwargs["allowed_updates"]
    assert {"callback_query", "message_reaction", "message_reaction_count", "chat_member", "business_message", "poll_answer"} <= set(
        subscribed
    )
    assert not {"inline_query", "chosen_inline_result"} & set(subscribed)
    assert not {"inline_query", "chosen_inline_result"} & set(application.dispatcher.resolve_used_update_types())
    assert next(method for method in session.methods if isinstance(method, DeleteWebhook)).drop_pending_updates is False
    assert session.closed


async def test_shutdown_drains_admitted_jobs_before_closing_dependencies(app_settings, boundaries):
    from msu_hub_bot.app import Application

    session, db = boundaries
    application = await Application.create(app_settings)
    started, finish = asyncio.Event(), asyncio.Event()

    async def worker():
        application.supervisor.admit_current_update()
        started.set()
        await finish.wait()

        async def late_job():
            assert not session.closed
            db.close.assert_not_awaited()

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


async def test_web_listener_starts_after_readiness_and_drains_before_database_close(app_settings, boundaries, monkeypatch):
    from aiogram.methods import SetChatMenuButton
    from msu_hub_bot import app

    events = []
    listener = AsyncMock()
    listener.start.side_effect = lambda: events.append("web-start")
    listener.close.side_effect = lambda: events.append("web-close")
    monkeypatch.setattr(app, "WebServer", lambda *args, **kwargs: listener)
    app_settings.web_app_url = "https://app.example.invalid"
    session, db = boundaries
    db.check.side_effect = lambda: events.append("database-ready")
    db.close.side_effect = lambda: events.append("database-close")
    application = await app.Application.create(app_settings)
    await application.start()
    assert application.dispatcher.workflow_data["reminders"] is application.reminders
    assert events == ["database-ready", "web-start"]
    menu = next(method for method in session.methods if isinstance(method, SetChatMenuButton))
    assert menu.menu_button.web_app.url == app_settings.web_app_url
    await application.close()
    assert events[-2:] == ["web-close", "database-close"]
