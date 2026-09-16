import asyncio
from abc import ABCMeta, abstractmethod
from contextlib import suppress
from dataclasses import dataclass, make_dataclass
from itertools import chain
from typing import Type

import gino
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from gino import Gino
from sqlalchemy import text

from common.config import ConfigBase
from common.executor import TPExecutor
from common.hc import HealthCheck
from common.logger import LoggerBuilder
from common.mixins import LoggerMixin
from common.tg.middlewares.logs import LoggingMiddleware
from common.tg.storage import RedisStorage
from common.tg.wrapper import BotWrapper
from common.utils import attributes, call
from common.vk.api import VkApi
from msu_hub_bot.integrations import UnavailableClient


class AppBase(LoggerMixin, metaclass=ABCMeta):
    config: ConfigBase = None

    def init_all(self, config: ConfigBase):
        self.config = config

        applets = []
        for cls in chain(self.__class__.__bases__, [self.__class__]):
            if hasattr(cls, 'init') and cls is not AppBase:
                applets.append(cls.__name__)
                cls.init(self)

        self.logger.info('Applets: ' + ', '.join(applets))
        self.logger.info('Configuration: ' + config.name)

        for cls in chain(self.__class__.__bases__, [self.__class__]):
            if hasattr(cls, 'post_init') and cls is not AppBase:
                cls.post_init(self)

        return self

    async def on_startup_all(self):
        coros = call(attributes(self.__class__.__bases__, 'on_startup'), self)
        return await asyncio.gather(*coros)

    async def on_shutdown_all(self):
        coros = call(attributes(self.__class__.__bases__, 'on_shutdown'), self)
        return await asyncio.gather(*coros)

    @abstractmethod
    def init(self):
        pass

    def post_init(self):
        pass

    async def on_startup(self):
        pass

    async def on_shutdown(self):
        pass


class AppLogger(AppBase):
    def init(self):
        config = self.config

        LoggerBuilder.set_defaults(config.logs_file.format(name=config.name), config.sentry_url)
        # Remove previous logger, another will be created at accessing
        del self.logger


@dataclass
class AppCPUExecutor(AppBase):
    cpu_executor: TPExecutor = None

    def init(self):
        self.cpu_executor = TPExecutor(max_workers=3)

    async def on_shutdown(self):
        self.cpu_executor.shutdown(wait=False)


@dataclass
class AppVK(AppBase):
    vk_api: VkApi = None

    def init(self):
        self.vk_api = VkApi(token=self.config.vk_user_token) if self.config.vk_user_token else UnavailableClient('vk_user_token')
        return self

    async def on_shutdown(self):
        await self.vk_api.close()


@dataclass
class AppBot(AppBase):
    bot: BotWrapper = None
    dp: Dispatcher = None
    redis: RedisStorage = None

    def init(self):
        config = self.config

        # Proxy
        proxy = config.proxy if config.proxy else None

        # Bot objects
        bot = BotWrapper(token=config.bot_token, proxy=proxy, parse_mode='html')
        if config.redis_host:
            storage = RedisStorage(host=config.redis_host,
                                   port=config.redis_port,
                                   password=config.redis_password,
                                   db=config.redis_db,
                                   prefix=config.name,
                                   pool_size=None)
        else:
            storage = MemoryStorage()
        dp = Dispatcher(bot, storage=storage)

        # Middlewares
        logging_middleware = LoggingMiddleware()
        dp.middleware.setup(logging_middleware)

        # Specify instance
        self.bot = bot
        self.dp = dp
        self.redis = storage
        return self

    async def on_startup(self):
        me = await self.bot.me
        await self.redis._redis.ping()
        self.logger.info(f'Bot Info: {me.first_name} (@{me.username}), ID: {me.id}')

    async def on_shutdown(self):
        closers = [
            self.bot.close(),
            self.dp.storage.close(),
            *call(attributes(self.dp.middleware.applications, 'close')),
        ]
        await asyncio.gather(*closers)
        await self.dp.storage.wait_closed()


@dataclass
class AppScheduler(AppBase):
    scheduler: AsyncIOScheduler = None

    def init(self):
        self.scheduler = AsyncIOScheduler()
        return self

    async def on_shutdown(self):
        self.scheduler.shutdown(wait=False)


def get_db_mixin(db: Gino, startup_requests: list = None):
    @dataclass
    class AppDatabase(AppBase):
        db: Gino = None
        startup_requests: list = None

        def init(self):
            self.db = db
            self.startup_requests = startup_requests or []
            return self

        async def on_startup(self):
            cfg = self.config
            db_url = f'postgresql://{cfg.pg_user}:{cfg.pg_password}@{cfg.pg_host}:{cfg.pg_port}/main'
            await self.db.set_bind(db_url)
            await self.db.gino.create_all()
            for request in self.startup_requests:
                await db.status(text(request))

        async def on_shutdown(self):
            bind = self.db.pop_bind()
            with suppress(gino.exceptions.UninitializedError):
                await bind.close()

    return AppDatabase


def app_class(class_name: str, *bases: Type[AppBase]):
    return make_dataclass(class_name, fields=None, bases=bases)


@dataclass
class AppHealthCheck(AppBase):
    hc: HealthCheck = None

    def init(self):
        self.hc = HealthCheck(self.config.health_check_url)
        return self

    async def on_startup(self):
        await self.hc.start()

    async def on_shutdown(self):
        await self.hc.stop()
