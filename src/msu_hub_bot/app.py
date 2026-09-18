"""Composition root: one poller, explicit services and ordered resource ownership."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, cast

from aiogram import Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode, UpdateType
from aiogram.filters import Command
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.redis import RedisStorage as FSMRedisStorage
from ccxt.async_support import binance
from redis.asyncio import Redis

from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.storage.factory import create_repository
from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.providers.dvach import Api2chAsync
from msu_hub_bot.uptime import HealthCheck
from msu_hub_bot.telegram.middlewares.check_gets import CheckGets
from msu_hub_bot.telegram.filters import MetaCommand
from msu_hub_bot.telegram.middlewares.logs import LoggingMiddleware
from msu_hub_bot.telegram.middlewares.settings import SettingsMiddleware
from msu_hub_bot.telegram.middlewares.skip777000 import Skip777000
from msu_hub_bot.telegram.middlewares.updates import UpdatesMiddleware
from msu_hub_bot.telegram.middlewares.telemetry import DispatchTelemetryMiddleware, HandlerTelemetryMiddleware
from msu_hub_bot.telegram.middlewares.viewer import ViewerMiddleware
from msu_hub_bot.telegram.runtime import AdmissionMiddleware, DrainTimeout, Supervisor
from msu_hub_bot.telegram.state import (
    ReleasableEventIsolation,
    SelectiveIsolationMiddleware,
    StateContextMiddleware,
    TopicFSMContextMiddleware,
)
from msu_hub_bot.telegram.storage import RedisStorage
from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.providers.vk.api import VkApi
from msu_hub_bot.commands.geoguess import Geoguess
from msu_hub_bot.commands.chess import Chess
from msu_hub_bot.events import EcosystemManager, EventsMiddleware
from msu_hub_bot.routing import build_router
from msu_hub_bot.providers.jdoodle import ManyJDoodle
from msu_hub_bot.providers.wit import Wit
from msu_hub_bot.providers.wolfram import WolframAPI
from msu_hub_bot.settings import Settings
from msu_hub_bot.telemetry import Backend, Telemetry, TelemetryConfig

logger = logging.getLogger(__name__)

# Subscribe to every supported kind, including passive history and reactions.
# Handler-derived defaults omit middleware-only events; Telegram's empty-list
# default also excludes reaction and membership changes.
ALLOWED_UPDATES: list[str] = [kind.value for kind in UpdateType]
SHUTDOWN_SECONDS = 85.0
DRAIN_SECONDS = 65.0


@dataclass
class Application:
    bot: BotWrapper
    dispatcher: Dispatcher
    supervisor: Supervisor
    database: BotRepository
    redis_client: Redis
    redis: RedisStorage
    fsm: TopicFSMContextMiddleware
    stack: AsyncExitStack
    health: HealthCheck
    telemetry: Telemetry
    _producer: asyncio.Task[None] | None = None
    _quiz_producer: asyncio.Task[None] | None = None
    _closed: bool = False

    @classmethod
    async def create(cls, settings: Settings, *, telemetry_config: TelemetryConfig | None = None) -> Application:
        """Allocate on a running loop; unwind every completed allocation on failure."""
        settings.validate_core()
        stack = AsyncExitStack()
        try:
            telemetry = Telemetry(telemetry_config)
            stack.push_async_callback(telemetry.close)
            session = AiohttpSession(proxy=settings.proxy or None, timeout=90)
            stack.push_async_callback(session.close)
            bot = BotWrapper(
                token=settings.bot_token, session=session, telemetry=telemetry, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
            )
            supervisor = Supervisor(telemetry=telemetry)
            client = Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                password=settings.redis_password or None,
                db=settings.redis_db,
                decode_responses=True,
            )
            # The real FSM owns this shared Redis client's close, including the pool.
            storage = FSMRedisStorage(
                client,
                key_builder=DefaultKeyBuilder(
                    prefix=f"{settings.name}:fsm3",
                    with_bot_id=True,
                    with_destiny=True,
                ),
            )
            isolation = ReleasableEventIsolation()
            fsm = TopicFSMContextMiddleware(storage, isolation)
            stack.push_async_callback(fsm.close)
            redis = RedisStorage(client, prefix=settings.name, supervisor=supervisor, telemetry=telemetry)
            database = create_repository(settings, telemetry=telemetry)
            stack.push_async_callback(database.close)
            executor = TPExecutor(max_workers=3, telemetry=telemetry)
            stack.push_async_callback(asyncio.to_thread, executor.shutdown, wait=True)
            vk_api = VkApi(token=settings.vk_user_token)
            stack.push_async_callback(vk_api.close)
            dvach = Api2chAsync()
            stack.push_async_callback(dvach.close)
            wit = Wit(settings.wit_tokens, executor=executor, telemetry=telemetry)
            stack.push_async_callback(wit.close)
            wolfram = WolframAPI(settings.wolfram_token, telemetry=telemetry)
            stack.push_async_callback(wolfram.close)
            jdoodle = ManyJDoodle(settings.jdoodle_tokens, telemetry=telemetry)
            stack.push_async_callback(jdoodle.close)
            crypto_exchange = binance()
            stack.push_async_callback(crypto_exchange.close)
            health = HealthCheck(settings.health_check_url)
            stack.push_async_callback(health.stop)
            backend = Backend(settings.storage_backend)
            preferences = SettingsMiddleware(database, telemetry=telemetry, backend=backend)
            stack.push_async_callback(preferences.close)
            stack.push_async_callback(Geoguess.shutdown)
            stack.push_async_callback(Chess.shutdown)
            ecosystem = EcosystemManager(bot, database)
            events = EventsMiddleware(bot, database, settings.events_chat_id, em=ecosystem)

            dispatcher = Dispatcher(disable_fsm=True)
            dispatcher.update.outer_middleware(AdmissionMiddleware(supervisor))
            dispatcher.update.outer_middleware(StateContextMiddleware())
            dispatcher.update.outer_middleware(LoggingMiddleware())
            dispatcher.update.outer_middleware(DispatchTelemetryMiddleware(telemetry))
            dispatcher.update.outer_middleware(UpdatesMiddleware(database, supervisor, telemetry=telemetry, backend=backend))
            dispatcher.update.outer_middleware(fsm)
            for kind, observer in dispatcher.observers.items():
                if kind not in ("update", "error"):
                    observer.outer_middleware(preferences)
                    observer.middleware(SelectiveIsolationMiddleware())
                    observer.middleware(HandlerTelemetryMiddleware(telemetry))
            # These automatic behaviors apply only to new messages. Running them
            # for edits or channel posts would repeat previews and membership work.
            dispatcher.message.outer_middleware(Skip777000())
            dispatcher.message.outer_middleware(CheckGets())
            dispatcher.message.outer_middleware(events)
            dispatcher.message.outer_middleware(ViewerMiddleware(bot, vk_api, executor))
            dispatcher.workflow_data.update(
                telemetry=telemetry,
                db=database,
                redis=redis,
                supervisor=supervisor,
                events_isolation=isolation,
                vk_api=vk_api,
                dvach=dvach,
                wit=wit,
                wolfram=wolfram,
                jdoodle=jdoodle,
                cpu_executor=executor,
                crypto_exchange=crypto_exchange,
                em=ecosystem,
                events_chat_id=settings.events_chat_id,
                posting_main_chat_id=settings.posting_main_chat_id,
                posting_tb_chat_id=settings.posting_tb_chat_id,
            )
            dispatcher.include_router(build_router(wit=wit, wolfram=wolfram, config=settings))
            telemetry.register_handlers(
                {
                    handler.flags["handler_key"]
                    for router in dispatcher.chain_tail
                    for observer in router.observers.values()
                    for handler in observer.handlers
                    if "handler_key" in handler.flags
                }
            )
            telemetry.register_commands(
                {
                    command
                    for router in dispatcher.chain_tail
                    for observer in router.observers.values()
                    for handler in observer.handlers
                    for filter_ in handler.filters or ()
                    if isinstance(filter_.callback, (MetaCommand, Command))
                    for command in filter_.callback.commands
                    if isinstance(command, str)
                }
            )
            return cls(bot, dispatcher, supervisor, database, client, redis, fsm, stack, health, telemetry)
        except BaseException:
            await stack.aclose()
            raise

    async def _deletion_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.redis.process_messages_to_delete(self.bot)
            except Exception:
                logger.exception("Scheduled deletion scan failed")

    async def _restore_quizzes(self) -> None:
        for quiz in (Geoguess, Chess):
            try:
                await quiz.restore(self.bot, self.redis, self.supervisor)
            except Exception:
                logger.exception("Quiz recovery scan failed")

    async def _quiz_recovery_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            await self._restore_quizzes()

    async def start(self) -> None:
        await self.telemetry.start()
        await self.database.check()
        await cast(Awaitable[bool], self.redis_client.ping())
        await self.bot.me()
        await self.bot.delete_webhook(drop_pending_updates=False)
        await self.health.start()
        await self._restore_quizzes()
        self._quiz_producer = asyncio.create_task(self._quiz_recovery_loop(), name="quiz-recovery")
        self._producer = asyncio.create_task(self._deletion_loop(), name="scheduled-deletions")

    async def close(self, *, hard_exit: Callable[[int], Any] = os._exit) -> None:
        if self._closed:
            return
        # An asyncio deadline cannot terminate arbitrary worker/exporter threads.
        # Keep a process backstop armed until all owned closers have returned.
        watchdog = threading.Timer(SHUTDOWN_SECONDS, hard_exit, args=(1,))
        watchdog.daemon = True
        watchdog.start()
        self.supervisor.close_updates()
        try:
            producers = [task for task in (self._producer, self._quiz_producer) if task is not None]
            for task in producers:
                task.cancel()
            await asyncio.gather(*producers, return_exceptions=True)
            await self.health.stop()
            try:
                await self.supervisor.drain(timeout=DRAIN_SECONDS, cancel_timeout=5)
            except DrainTimeout:
                logger.error("Application workers exceeded the shutdown deadline")
                hard_exit(1)
                raise  # Test doubles must not let live consumers use closed clients.
            await self.stack.aclose()
            self._closed = True
        finally:
            watchdog.cancel()

    async def run(self) -> None:
        try:
            await self.start()
            await self.dispatcher.start_polling(
                self.bot,
                polling_timeout=60,
                handle_as_tasks=True,
                allowed_updates=ALLOWED_UPDATES,
                close_bot_session=False,
            )
        finally:
            await self.close()


async def run(settings: Settings, *, telemetry_config: TelemetryConfig | None = None) -> None:
    app = await Application.create(settings, telemetry_config=telemetry_config)
    await app.run()
