"""Composition root: one poller, explicit services and ordered resource ownership."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from aiogram import Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode, UpdateType
from aiogram.filters import Command
from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiogram.utils.token import extract_bot_id
from ccxt.async_support import binance

from msu_hub_bot.storage.base import BotRepository
from msu_hub_bot.storage.factory import create_repository
from msu_hub_bot.storage.features import FeatureStore, FeatureWorker
from msu_hub_bot.games import QuizService
from msu_hub_bot.games.quest import QuestService
from msu_hub_bot.providers.quest import QuestProvider
from msu_hub_bot.games.raffle import RaffleStore
from msu_hub_bot.games.chess_play.service import ChessMatchService
from msu_hub_bot.reminders import ReminderService
from msu_hub_bot.feedback import FeedbackService
from msu_hub_bot.feedback.context import DiagnosticBuffer
from msu_hub_bot.web.links import WebAppLinks
from msu_hub_bot.web.server import WebServer
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
from msu_hub_bot.telegram.middlewares.viewer import ViewerMiddleware, preview_policy
from msu_hub_bot.telegram.runtime import AdmissionMiddleware, DrainTimeout, Supervisor
from msu_hub_bot.telegram.state import (
    ReleasableEventIsolation,
    SelectiveIsolationMiddleware,
    StateContextMiddleware,
    TopicFSMContextMiddleware,
)
from msu_hub_bot.telegram.fsm_storage import FeatureFSMStorage
from msu_hub_bot.telegram.deletions import MessageDeletions
from msu_hub_bot.telegram.membership_inbox import MembershipInbox
from msu_hub_bot.telegram.wrapper import BotWrapper
from msu_hub_bot.providers.vk.api import VkApi
from msu_hub_bot.events import EcosystemManager, EventsMiddleware
from msu_hub_bot.routing import build_router
from msu_hub_bot.providers.jdoodle import ManyJDoodle
from msu_hub_bot.providers.jev import JevClient
from msu_hub_bot.commands.intents import IntentCommands
from msu_hub_bot.providers.wit import Wit
from msu_hub_bot.providers.wolfram import WolframAPI
from msu_hub_bot.settings import Settings
from msu_hub_bot.telemetry import Backend, Telemetry, TelemetryConfig

logger = logging.getLogger(__name__)

# Retain passive history and reactions while excluding disabled inline mode.
# Handler-derived defaults omit middleware-only events; Telegram's empty-list
# default also excludes reaction and membership changes.
ALLOWED_UPDATES: list[str] = [kind.value for kind in UpdateType if kind not in {UpdateType.INLINE_QUERY, UpdateType.CHOSEN_INLINE_RESULT}]
SHUTDOWN_SECONDS = 85.0
DRAIN_SECONDS = 65.0


@dataclass
class Application:
    bot: BotWrapper
    dispatcher: Dispatcher
    supervisor: Supervisor
    database: BotRepository
    deletions: MessageDeletions
    fsm: TopicFSMContextMiddleware
    stack: AsyncExitStack
    health: HealthCheck
    telemetry: Telemetry
    features: FeatureStore
    feature_worker: FeatureWorker
    quiz: QuizService
    chess_matches: ChessMatchService
    quests: QuestService
    reminders: ReminderService
    web_apps: WebAppLinks
    membership_inbox: MembershipInbox
    web: WebServer | None = None
    _feature_task: asyncio.Task[None] | None = None
    _membership_task: asyncio.Task[None] | None = None
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
            supervisor = Supervisor(telemetry=telemetry)
            database = create_repository(settings, telemetry=telemetry)
            stack.push_async_callback(database.close)
            membership_inbox = MembershipInbox(
                settings.membership_inbox_path, extract_bot_id(settings.bot_token), database, telemetry=telemetry
            )
            stack.push_async_callback(membership_inbox.close)
            bot = BotWrapper(
                token=settings.bot_token,
                session=session,
                telemetry=telemetry,
                membership_inbox=membership_inbox,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
            features = FeatureStore(database)
            feature_worker = FeatureWorker(features, telemetry=telemetry)
            storage = FeatureFSMStorage(features)
            isolation = ReleasableEventIsolation()
            fsm = TopicFSMContextMiddleware(storage, isolation)
            stack.push_async_callback(fsm.close)
            deletions = MessageDeletions(features, bot, feature_worker)
            quiz = QuizService(bot, features, feature_worker)
            raffles = RaffleStore(bot.id, features)
            chess_matches = ChessMatchService(bot, features, feature_worker)
            quest_provider = QuestProvider()
            stack.push_async_callback(quest_provider.close)
            quests = QuestService(bot, features, feature_worker, quest_provider)
            reminders = ReminderService(bot, features, feature_worker)
            web_apps = WebAppLinks(bot.token, settings.web_app_url)
            feedback = FeedbackService(
                bot,
                features,
                feature_worker,
                destination_chat_id=settings.events_chat_id,
                reviewer_ids=(settings.owner_id,) if settings.owner_id > 0 else (),
                review_button=web_apps.feedback_button,
            )
            diagnostics = DiagnosticBuffer(release=telemetry.config.release)
            executor = TPExecutor(max_workers=3, telemetry=telemetry)
            stack.push_async_callback(asyncio.to_thread, executor.shutdown, wait=True)
            vk_api = VkApi(token=settings.vk_user_token)
            stack.push_async_callback(vk_api.close)
            backend = Backend(settings.storage_backend)
            preferences = SettingsMiddleware(database, telemetry=telemetry, backend=backend)
            stack.push_async_callback(preferences.close)
            web = (
                WebServer(
                    bot,
                    reminders,
                    database,
                    web_apps,
                    telemetry,
                    port=settings.web_port,
                    vk_api=vk_api,
                    settings_changed=preferences.invalidate,
                    feedback=feedback,
                )
                if settings.web_app_url
                else None
            )
            dvach = Api2chAsync()
            stack.push_async_callback(dvach.close)
            wit = Wit(settings.wit_tokens, executor=executor, telemetry=telemetry)
            stack.push_async_callback(wit.close)
            wolfram = WolframAPI(settings.wolfram_token, telemetry=telemetry)
            stack.push_async_callback(wolfram.close)
            jdoodle = ManyJDoodle(settings.jdoodle_tokens, telemetry=telemetry)
            stack.push_async_callback(jdoodle.close)
            intents = None
            if settings.jev_enabled:
                jev = JevClient(settings.openrouter_api_key)
                stack.push_async_callback(jev.close)
                intents = IntentCommands(jev, telemetry=telemetry, confidence=settings.jev_confidence)
            crypto_exchange = binance()
            stack.push_async_callback(crypto_exchange.close)
            health = HealthCheck(settings.health_check_url)
            stack.push_async_callback(health.stop)
            ecosystem = EcosystemManager(bot, database, telemetry=telemetry)
            events = EventsMiddleware(bot, database, settings.events_chat_id, em=ecosystem)
            skip_automatic_forward = Skip777000(bot_id=bot.id, telemetry=telemetry)

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
                    observer.middleware(HandlerTelemetryMiddleware(telemetry, diagnostics=diagnostics))
            # These automatic behaviors apply only to new messages. Running them
            # for edits or channel posts would repeat previews and membership work.
            dispatcher.message.outer_middleware(skip_automatic_forward)
            dispatcher.my_chat_member.outer_middleware(skip_automatic_forward)
            dispatcher.my_chat_member.outer_middleware(events)
            dispatcher.message.outer_middleware(CheckGets())
            dispatcher.message.outer_middleware(events)
            dispatcher.message.outer_middleware(ViewerMiddleware(bot, vk_api, executor, telemetry=telemetry))
            dispatcher.message.middleware(preview_policy)
            dispatcher.workflow_data.update(
                telemetry=telemetry,
                db=database,
                deletions=deletions,
                supervisor=supervisor,
                quiz=quiz,
                raffles=raffles,
                chess_matches=chess_matches,
                quests=quests,
                reminders=reminders,
                feedback=feedback,
                feedback_diagnostics=diagnostics,
                web_apps=web_apps,
                events_isolation=isolation,
                vk_api=vk_api,
                dvach=dvach,
                wit=wit,
                wolfram=wolfram,
                jdoodle=jdoodle,
                intents=intents,
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
            return cls(
                bot,
                dispatcher,
                supervisor,
                database,
                deletions,
                fsm,
                stack,
                health,
                telemetry,
                features,
                feature_worker,
                quiz,
                chess_matches,
                quests,
                reminders,
                web_apps,
                membership_inbox,
                web,
            )
        except BaseException:
            await stack.aclose()
            raise

    async def start(self) -> None:
        await self.telemetry.start()
        await self.database.check()
        await self.features.check()
        await self.membership_inbox.open()
        identity = await self.bot.me()
        self.web_apps.username = identity.username or ""
        if self.web is not None:
            await self.web.start()
            await self.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="MSU Hub", web_app=WebAppInfo(url=self.web_apps.url)), request_timeout=15
            )
        await self.bot.delete_webhook(drop_pending_updates=False)
        await self.health.start()
        self._membership_task = self.supervisor.create_job(self.membership_inbox.run, trace=False)
        self._feature_task = self.supervisor.create_job(self.feature_worker.run, trace=False)

    async def close(self, *, hard_exit: Callable[[int], Any] = os._exit) -> None:
        if self._closed:
            return
        # An asyncio deadline cannot terminate arbitrary worker/exporter threads.
        # Keep a process backstop armed until all owned closers have returned.
        watchdog = threading.Timer(SHUTDOWN_SECONDS, hard_exit, args=(1,))
        watchdog.daemon = True
        watchdog.start()
        self.supervisor.close_updates()
        self.membership_inbox.stop()
        self.feature_worker.stop()
        drain_deadline = asyncio.get_running_loop().time() + DRAIN_SECONDS
        try:
            if self.web is not None:
                await self.web.close()
            await self.health.stop()
            try:
                await self.supervisor.drain(timeout=max(0, drain_deadline - asyncio.get_running_loop().time()), cancel_timeout=5)
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
