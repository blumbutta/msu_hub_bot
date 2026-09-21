"""Bounded Meander story loading; the public book contract has no Telegram state."""

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from uuid import UUID
from typing import Protocol

import aiohttp
from pydantic import JsonValue

from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.http import USER_AGENT, read_limited

DEFAULT_STORY_ID = "demo"
TRUST_STORY_ID = "baa3d4e5-8979-43b3-ad41-ea63d08b8628"
FETCH_TIMEOUT = 8.0
CACHE_TTL = 900.0
CACHE_BOOKS = 2
API_URL = "https://meander.sbs/api/be/quests"


class QuestError(ExternalServiceError):
    """The story is unavailable, incompatible, or no longer matches its saved version."""


@dataclass(frozen=True)
class QuestScene:
    text: str
    choices: tuple[str, ...]
    image: bytes | None = None


class QuestBook(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def author(self) -> str: ...

    @property
    def digest(self) -> str: ...

    def start(self) -> dict[str, JsonValue]: ...

    def view(self, state: dict[str, JsonValue]) -> QuestScene: ...

    def choose(self, state: dict[str, JsonValue], index: int) -> dict[str, JsonValue]: ...


class QuestProvider:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._cache: OrderedDict[str, tuple[float, QuestBook]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._parse_task: asyncio.Task[QuestBook] | None = None
        self._closed = False

    async def load(self, story_id: str, expected_hash: str | None = None) -> QuestBook:
        """A saved digest must match exactly, including after process restart."""
        if self._closed:
            raise QuestError("Источник квестов уже закрыт.")
        if expected_hash is not None and (len(expected_hash) != 64 or any(char not in "0123456789abcdef" for char in expected_hash)):
            raise QuestError("Сохранённая версия квеста повреждена.")
        if story_id == DEFAULT_STORY_ID:
            from msu_hub_bot.providers.quest_demo import demo_book

            book: QuestBook = demo_book()
            return self._pin(book, expected_hash)
        try:
            if str(UUID(story_id)) != story_id:
                raise ValueError("Non-canonical quest id")
        except (ValueError, AttributeError) as exc:
            raise QuestError("Укажите ID квеста из Meander или demo.") from exc
        try:
            async with asyncio.timeout(FETCH_TIMEOUT):
                async with self._lock:
                    cached = self._cache.get(story_id)
                    if cached is not None and (cached[0] > time.monotonic() or expected_hash == cached[1].digest):
                        self._cache.move_to_end(story_id)
                        return self._pin(cached[1], expected_hash)
                    # A timed-out parse still owns its worker. Do not stack workers
                    # when slow image decoding outlives the caller's deadline.
                    if self._parse_task is not None:
                        previous = await asyncio.gather(asyncio.shield(self._parse_task), return_exceptions=True)
                        del previous
                        self._parse_task = None
                    body = await self._download(story_id)
                    self._parse_task = asyncio.create_task(self._parse(body, story_id))
                    self._parse_task.add_done_callback(_consume_failure)
                    book = await asyncio.shield(self._parse_task)
                    self._parse_task = None
                    self._pin(book, expected_hash)
                    self._cache[story_id] = (time.monotonic() + CACHE_TTL, book)
                    self._cache.move_to_end(story_id)
                    while len(self._cache) > CACHE_BOOKS:
                        self._cache.popitem(last=False)
                    return book
        except QuestError:
            raise
        except (aiohttp.ClientError, TimeoutError, ExternalServiceError) as exc:
            raise QuestError("Не удалось загрузить квест за 8 секунд. Попробуйте ещё раз.") from exc

    @staticmethod
    def _pin(book: QuestBook, expected_hash: str | None) -> QuestBook:
        if expected_hash is not None and book.digest != expected_hash:
            raise QuestError("Автор обновил квест. Старую партию нельзя продолжить с другой версией.")
        return book

    async def _download(self, story_id: str) -> bytes:
        from msu_hub_bot.providers.quest_runtime import MAX_ARCHIVE_BYTES

        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
                headers={"User-Agent": USER_AGENT},
            )
        # No user-controlled host, remote asset URLs or redirect targets are used.
        async with self._session.get(f"{API_URL}/{story_id}/file", allow_redirects=False) as response:
            return await read_limited(response, MAX_ARCHIVE_BYTES)

    @staticmethod
    async def _parse(body: bytes, story_id: str) -> QuestBook:
        from msu_hub_bot.providers.quest_runtime import parse_mnd

        return await asyncio.to_thread(parse_mnd, body, story_id)

    async def close(self) -> None:
        self._closed = True
        if self._session is not None:
            await self._session.close()
        if self._parse_task is not None:
            await asyncio.gather(self._parse_task, return_exceptions=True)
            self._parse_task = None
        self._cache.clear()


def _consume_failure(task: asyncio.Task[QuestBook]) -> None:
    if not task.cancelled():
        task.exception()
