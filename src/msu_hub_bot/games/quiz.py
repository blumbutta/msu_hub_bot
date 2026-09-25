"""Durable, message-based quizzes shared by community game commands."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import EditMessageCaption, EditMessageMedia, TelegramMethod
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message
from aiogram.utils.formatting import Text
from aiogram.utils.keyboard import InlineKeyboardBuilder
from cachetools import LRUCache

from msu_hub_bot.commands.quiz_view import View
from msu_hub_bot.games.definitions import DEFINITIONS, Definition
from msu_hub_bot.games.models import ChatState, RoundState, Score, Vote
from msu_hub_bot.games.scores import DAY_ZONE, SCORE_BATCH_SIZE, apply_batch, ranking
from msu_hub_bot.storage.features import (
    Collection,
    Conflict,
    FeatureError,
    FeatureStore,
    FeatureWorker,
    JobContext,
    JobHold,
    JobRetry,
    Record,
    RecordKey,
    Scope,
    Transaction,
)
from msu_hub_bot.storage.supabase import RepositoryError, RepositoryUnavailable
from msu_hub_bot.telemetry import record_handled_failure

logger = logging.getLogger(__name__)
SEND_TIMEOUT = 15
PHOTO_TIMEOUT = 10
ROUND_TIMEOUT = timedelta(minutes=10)
PUBLICATION_TIMEOUT = timedelta(minutes=1)
RESULT_TTL = timedelta(days=1)
EDIT_INTERVAL = 1.0
MAX_VOTES = 10_000
_Result = TypeVar("_Result")


@dataclass(frozen=True)
class Collections:
    chats: Collection[ChatState]
    rounds: Collection[RoundState]
    votes: Collection[Vote]
    scores: Collection[Score]


@dataclass
class Presentation:
    view: View | None = None
    markup: InlineKeyboardMarkup | None = None
    last_edit: float = 0.0
    solution_shown: bool = False


@dataclass
class LocalLock:
    lock: asyncio.Lock
    users: int = 0


class QuizService:
    """Database state is authoritative; restarting loses only rendering caches."""

    def __init__(self, bot: Bot, store: FeatureStore, worker: FeatureWorker) -> None:
        self.bot, self.store, self.worker = bot, store, worker
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.collections: dict[str, Collections] = {}
        self._locks: dict[tuple[str, int], LocalLock] = {}
        self._presentations: LRUCache[tuple[str, int, str], Presentation] = LRUCache(maxsize=1024)
        self._providers = asyncio.Semaphore(8)
        for feature in DEFINITIONS:
            self.collections[feature] = Collections(
                store.collection(feature, "chats", ChatState, retention=timedelta(days=30)),
                store.collection(feature, "rounds", RoundState, retention=None),
                store.collection(feature, "votes", Vote, retention=None),
                store.collection(feature, "scores", Score, retention=None),
            )
            worker.register(feature, "deadline", self._retryable(self._deadline), max_attempts=1024)
            worker.register(feature, "settle", self._retryable(self._settle), max_attempts=1024)
            worker.register(feature, "render", self._retryable(self._render), max_attempts=1024)
            worker.register(feature, "cleanup", self._retryable(self._cleanup), max_attempts=1024)

    @staticmethod
    def _retryable(handler: Callable[[JobContext], Awaitable[None]]) -> Callable[[JobContext], Awaitable[None]]:
        async def run(context: JobContext) -> None:
            try:
                await handler(context)
            except Conflict, RepositoryUnavailable, TimeoutError, TelegramAPIError:
                # These jobs only perform conditional database writes, idempotent
                # score settlement or edits of one known Telegram message.
                raise JobRetry("Quiz work can safely be replayed") from None

        return run

    async def _send(self, method: TelegramMethod[_Result]) -> _Result:
        async with asyncio.timeout(SEND_TIMEOUT):
            return await self.bot(method, request_timeout=SEND_TIMEOUT)

    def _tx(self, feature: str, scope: Scope) -> Transaction:
        return self.store.transaction(feature, scope, operation_id=uuid4().hex)

    @staticmethod
    async def _commit(tx: Transaction) -> None:
        try:
            await tx.commit()
        except RepositoryUnavailable, TimeoutError:
            # Replay exactly the frozen request, including its receipt identity.
            await tx.commit()

    def _lock(self, feature: str, chat_id: int) -> asyncio.Lock:
        entry = self._locks.setdefault((feature, chat_id), LocalLock(asyncio.Lock()))
        entry.users += 1
        return entry.lock

    def _unlock(self, feature: str, chat_id: int, lock: asyncio.Lock) -> None:
        entry = self._locks.get((feature, chat_id))
        if entry is not None and entry.lock is lock:
            entry.users -= 1
            if entry.users == 0:
                self._locks.pop((feature, chat_id), None)

    @staticmethod
    def _scope(chat_id: int) -> Scope:
        return Scope(f"chat:{chat_id}")

    @staticmethod
    def _token(bot_id: int, chat_id: int, message_id: int) -> str:
        return hashlib.blake2s(f"{bot_id}:{chat_id}:{message_id}".encode(), digest_size=6).hexdigest()

    async def round(self, feature: str, chat_id: int, token: str) -> Record[RoundState] | None:
        return await self.collections[feature].rounds.get(self._scope(chat_id), token)

    async def ranking(self, feature: str, chat_id: int) -> Text:
        day = self.clock().astimezone(DAY_ZONE).date()
        return await ranking(self.collections[feature].scores, self._scope(chat_id), day)

    async def votes(self, feature: str, scope: Scope, token: str) -> list[Record[Vote]]:
        result: list[Record[Vote]] = []
        after = None
        while True:
            page = await self.collections[feature].votes.list(scope, parent=token, after=after, limit=200)
            result.extend(page)
            if len(result) > MAX_VOTES:
                raise JobHold("Quiz participant limit exceeded")
            if len(page) < 200:
                return sorted(result, key=lambda record: (record.value.accepted_at, record.created_at, record.key))
            after = page[-1].key

    def _schedule_render(self, tx: Transaction, record: Record[RoundState]) -> None:
        tx.schedule(f"render:{record.key}", "render", record=RecordKey("rounds", record.key), run_at=self.clock())

    def _schedule_cleanup(self, tx: Transaction, record: Record[RoundState], *, immediate: bool = False) -> None:
        closed = record.value.closed_at or self.clock()
        tx.schedule(
            f"cleanup:{record.key}",
            "cleanup",
            record=RecordKey("rounds", record.key),
            run_at=self.clock() if immediate else max(self.clock(), closed + RESULT_TTL),
        )

    async def start(self, feature: str, message: Message) -> Message | None:
        scope, definitions = self._scope(message.chat.id), DEFINITIONS[feature]
        publication_deadline = (
            asyncio.get_running_loop().time() + definitions.photo_timeout if definitions.photo_timeout is not None else None
        )
        collections = self.collections[feature]
        token = self._token(self.bot.id, message.chat.id, message.message_id)
        lock = self._lock(feature, message.chat.id)
        record: Record[RoundState] | None = None
        try:
            async with asyncio.timeout_at(publication_deadline), lock:
                for _ in range(8):
                    chat = await collections.chats.get(scope, "state")
                    existing = await collections.rounds.get(scope, token)
                    if (chat is not None and chat.value.active is not None) or existing is not None:
                        return await self._send(message.reply("Подождите, прошлое задание еще не окончено!"))
                    state = RoundState(
                        token=token,
                        chat_id=message.chat.id,
                        thread_id=message.message_thread_id,
                        prepared_at=self.clock(),
                    )
                    chat_value = ChatState() if chat is None else chat.value.model_copy(deep=True)
                    chat_value.active = token
                    tx = self._tx(feature, scope)
                    if chat is None:
                        tx.expect_absent("chats", "state")
                    else:
                        tx.expect(chat)
                    tx.expect_absent("rounds", token)
                    tx.put(collections.chats, "state", chat_value, expires_at=None)
                    tx.put(collections.rounds, token, state, status="preparing")
                    tx.schedule(
                        f"deadline:{token}",
                        "deadline",
                        record=RecordKey("rounds", token),
                        run_at=state.prepared_at + PUBLICATION_TIMEOUT,
                    )
                    try:
                        await self._commit(tx)
                        record = await collections.rounds.get(scope, token)
                        break
                    except Conflict:
                        continue
                if record is None:
                    raise Conflict()
        except (FeatureError, RepositoryError, TimeoutError) as error:
            record_handled_failure(error)
            if isinstance(error, TimeoutError) and publication_deadline is not None:
                return await self._send(message.reply("Ошибка, попробуйте еще раз"))
            return await self._send(message.reply("Не удалось сохранить игру. Попробуй чуть позже."))
        finally:
            self._unlock(feature, message.chat.id, lock)

        sending = False
        try:
            deadline = publication_deadline if publication_deadline is not None else asyncio.get_running_loop().time() + PHOTO_TIMEOUT
            async with asyncio.timeout_at(deadline), self._providers:
                chat = await collections.chats.get(scope, "state")
                question = await definitions.load([] if chat is None else chat.value.recent)
                photo = await definitions.photo(question)
                state = record.value.model_copy(deep=True)
                state.question, state.phase = question, "publishing"
                tx = self._tx(feature, scope)
                tx.expect(record)
                tx.put(collections.rounds, token, state, status="publishing")
                await self._commit(tx)
                view = definitions.render(state, [])
                markup = self.keyboard(definitions, state, view)
                sending = True
                sent = await self._send(
                    message.reply_photo(
                        photo,
                        caption=view.caption,
                        caption_entities=view.entities,
                        parse_mode=None,
                        reply_markup=markup,
                    )
                )
                await self._bind(feature, scope, token, sent, self.clock())
                self._presentations[feature, message.chat.id, token] = Presentation(
                    view=view,
                    markup=markup,
                    last_edit=asyncio.get_running_loop().time(),
                )
        except asyncio.CancelledError:
            if not sending:
                await asyncio.shield(self._abandon(feature, scope, token))
            raise
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            record_handled_failure(error)
            await self._abandon(feature, scope, token)
            return await self._send(message.reply("Ошибка, попробуйте еще раз"))
        except Exception as error:
            record_handled_failure(error)
            if not sending:
                await self._abandon(feature, scope, token)
                return await self._send(message.reply("Ошибка, попробуйте еще раз"))
            # The photo may exist even when Telegram's acknowledgement was lost.
            logger.warning("Quiz publication could not be confirmed")
            return await self._send(
                message.reply(
                    "Не удалось подтвердить отправку. Если фото появилось, нажми на его кнопку — игра продолжится. "
                    "Если нет, попробуй снова через минуту."
                )
            )
        return None

    async def _bind(self, feature: str, scope: Scope, token: str, message: Message, published_at: datetime) -> None:
        collections = self.collections[feature]
        for _ in range(8):
            record = await collections.rounds.get(scope, token)
            chat = await collections.chats.get(scope, "state")
            if record is None or chat is None or chat.value.active != token:
                return
            if record.value.message_id is not None:
                return
            if record.value.phase != "publishing" or record.value.question is None:
                return
            state = record.value.model_copy(deep=True)
            assert state.question is not None
            state.phase, state.message_id = "active", message.message_id
            state.published_at, state.deadline_at = published_at, published_at + ROUND_TIMEOUT
            chat_value = chat.value.model_copy(deep=True)
            chat_value.recent = [*chat_value.recent, state.question.identity][-15:]
            tx = self._tx(feature, scope)
            tx.expect(record)
            tx.expect(chat)
            tx.put(collections.rounds, token, state, status="active")
            tx.put(collections.chats, "state", chat_value, expires_at=None)
            tx.schedule(f"deadline:{token}", "deadline", record=RecordKey("rounds", token), run_at=state.deadline_at)
            try:
                await self._commit(tx)
                return
            except Conflict:
                continue
        raise Conflict()

    async def _abandon(self, feature: str, scope: Scope, token: str) -> None:
        collections = self.collections[feature]
        for _ in range(8):
            record = await collections.rounds.get(scope, token)
            if record is None or record.value.phase not in {"preparing", "publishing"}:
                return
            chat = await collections.chats.get(scope, "state")
            state = record.value.model_copy(deep=True)
            state.phase, state.closed_at, state.score_status = "abandoned", self.clock(), "skipped"
            tx = self._tx(feature, scope)
            tx.expect(record)
            tx.put(collections.rounds, token, state, status="abandoned")
            tx.cancel_job(f"deadline:{token}")
            if chat is not None and chat.value.active == token:
                chat_value = chat.value.model_copy(deep=True)
                chat_value.active = None
                tx.expect(chat)
                tx.put(collections.chats, "state", chat_value)
            self._schedule_cleanup(tx, record, immediate=True)
            try:
                await self._commit(tx)
                return
            except Conflict:
                continue
        raise Conflict()

    async def callback(self, feature: str, query: CallbackQuery, token: str, choice: str) -> bool | None:
        if not isinstance(query.message, Message):
            return await self._send(query.answer("Этот раунд недоступен."))
        message, scope = query.message, self._scope(query.message.chat.id)
        lock = self._lock(feature, message.chat.id)
        try:
            async with lock:
                for _ in range(8):
                    record = await self.collections[feature].rounds.get(scope, token)
                    if record is not None and record.value.phase == "publishing" and record.value.message_id is None:
                        # Only Telegram's own callback Message can reconcile a lost send reply.
                        if self._recovery_message(feature, record.value, message):
                            await self._bind(feature, scope, token, message, message.date)
                            record = await self.collections[feature].rounds.get(scope, token)
                    if (
                        record is None
                        or record.value.chat_id != message.chat.id
                        or record.value.message_id != message.message_id
                        or record.value.phase not in {"active", "closed"}
                        or (record.value.closed_at is not None and self.clock() >= record.value.closed_at + RESULT_TTL)
                    ):
                        return await self._send(query.answer(f"Раунд недоступен. Начни новый: /{feature}", show_alert=True))
                    state = record.value
                    if state.phase == "active" and state.deadline_at is not None and self.clock() >= state.deadline_at:
                        await self._close(record, state.deadline_at)
                        continue
                    if choice.startswith("page_"):
                        value = choice.removeprefix("page_")
                        if not value.isascii() or not value.isdecimal() or len(value) > 8:
                            return await self._send(query.answer("Неизвестная страница."))
                        changed = state.model_copy(deep=True)
                        changed.page = int(value)
                        tx = self._tx(feature, scope)
                        tx.expect(record)
                        tx.put(self.collections[feature].rounds, token, changed, status=changed.phase)
                        self._schedule_render(tx, record)
                        try:
                            await self._commit(tx)
                            return await self._send(query.answer())
                        except Conflict:
                            continue
                    if state.phase == "closed":
                        tx = self._tx(feature, scope)
                        tx.expect(record)
                        self._schedule_render(tx, record)
                        try:
                            await self._commit(tx)
                            return await self._send(query.answer(f"Раунд завершён. Начни новый: /{feature}", show_alert=True))
                        except Conflict:
                            continue
                    if choice == "finish":
                        await self._close(record, self.clock())
                        return await self._send(query.answer("Задание завершено!"))
                    try:
                        index = int(choice)
                        if not 0 <= index < 6:
                            raise ValueError
                    except ValueError:
                        return await self._send(query.answer("Неизвестный вариант."))
                    user = query.from_user
                    key = f"{token}:{user.id}"
                    if await self.collections[feature].votes.get(scope, key) is not None:
                        return await self._send(query.answer("Твой ответ уже принят. Изменить его нельзя.", show_alert=True))
                    if state.vote_count >= MAX_VOTES:
                        return await self._send(query.answer("В этом раунде уже слишком много ответов.", show_alert=True))
                    vote = Vote(user_id=user.id, choice=index, name=user.full_name, username=user.username, accepted_at=self.clock())
                    changed = state.model_copy(deep=True)
                    changed.vote_count += 1
                    tx = self._tx(feature, scope)
                    tx.expect(record)
                    tx.expect_absent("votes", key)
                    tx.put(self.collections[feature].rounds, token, changed, status="active")
                    tx.put(self.collections[feature].votes, key, vote, parent=token)
                    self._schedule_render(tx, record)
                    try:
                        await self._commit(tx)
                        return await self._send(query.answer("Ответ принят! Результат — в конце раунда."))
                    except Conflict:
                        continue
                raise Conflict()
        except FeatureError, RepositoryError, TimeoutError:
            return await self._send(query.answer("Не удалось подтвердить запись. Попробуй нажать ещё раз.", show_alert=True))
        finally:
            self._unlock(feature, message.chat.id, lock)

    def _recovery_message(self, feature: str, state: RoundState, message: Message) -> bool:
        if (
            message.from_user is None
            or message.from_user.id != self.bot.id
            or not message.photo
            or message.chat.id != state.chat_id
            or message.message_thread_id != state.thread_id
            or state.question is None
            or message.date < state.prepared_at - timedelta(seconds=1)
            or message.date > self.clock()
        ):
            return False
        definitions = DEFINITIONS[feature]
        expected = self.keyboard(definitions, state, definitions.render(state, []))
        return (
            message.reply_markup is not None
            and expected is not None
            and message.reply_markup.model_dump(mode="json") == expected.model_dump(mode="json")
        )

    async def _close(self, record: Record[RoundState], closed_at: datetime) -> None:
        feature, scope, token = record.feature, record.scope, record.key
        collections = self.collections[feature]
        chat = await collections.chats.get(scope, "state")
        state = record.value.model_copy(deep=True)
        if state.phase != "active":
            return
        state.phase, state.closed_at, state.page = "closed", closed_at, 0
        state.score_day = closed_at.astimezone(DAY_ZONE).date()
        tx = self._tx(feature, scope)
        tx.expect(record)
        tx.put(collections.rounds, token, state, status="closed")
        if chat is not None and chat.value.active == token:
            chat_value = chat.value.model_copy(deep=True)
            chat_value.active = None
            tx.expect(chat)
            tx.put(collections.chats, "state", chat_value)
        tx.cancel_job(f"deadline:{token}")
        tx.schedule(
            f"settle:{token}",
            "settle",
            record=RecordKey("rounds", token),
            run_at=self.clock(),
            serial_key=f"scores:{state.score_day.isoformat()}",
        )
        self._schedule_render(tx, record)
        # Use the new closing time; the input record was still open.
        tx.schedule(
            f"cleanup:{token}",
            "cleanup",
            record=RecordKey("rounds", token),
            run_at=closed_at + RESULT_TTL,
        )
        await self._commit(tx)

    async def _deadline(self, context: JobContext) -> None:
        job = context.job
        record = await self.collections[job.feature].rounds.get(job.scope, job.record.key)
        if record is None or not await context.current():
            return
        if record.value.phase in {"preparing", "publishing"}:
            await self._abandon(job.feature, job.scope, record.key)
        elif record.value.phase == "active" and record.value.deadline_at is not None:
            await self._close(record, record.value.deadline_at)

    async def _settle(self, context: JobContext) -> None:
        job, collections = context.job, self.collections[context.job.feature]
        record = await collections.rounds.get(job.scope, job.record.key)
        if record is None or record.value.phase != "closed" or record.value.score_status != "pending":
            return
        state = record.value
        if state.score_day is None or state.question is None:
            raise JobHold("Quiz settlement has incomplete state")
        day, question = state.score_day, state.question
        # Closure freezes votes. Validate the whole set before any batch, then
        # guard every consumed vote alongside its score and the progress cursor.
        votes = sorted(await self.votes(job.feature, job.scope, record.key), key=lambda vote: vote.key)
        if len(votes) != state.vote_count or any(vote.key != f"{record.key}:{vote.value.user_id}" for vote in votes):
            raise JobHold("Quiz settlement has incomplete votes")
        while True:
            record = await collections.rounds.get(job.scope, job.record.key)
            if record is None or record.value.phase != "closed" or record.value.score_status != "pending":
                return
            state = record.value
            if state.score_day != day or state.question != question or state.vote_count != len(votes):
                raise JobHold("Quiz settlement snapshot changed")
            count = state.score_count
            if count > len(votes) or state.score_cursor != (votes[count - 1].key if count else None):
                raise JobHold("Quiz settlement progress is inconsistent")
            batch = votes[count : count + SCORE_BATCH_SIZE]
            changed = state.model_copy(deep=True)
            changed.score_count += len(batch)
            if batch:
                changed.score_cursor = batch[-1].key
            finished = changed.score_count == len(votes)
            if finished:
                changed.score_status = "recorded"
            tx = self._tx(job.feature, job.scope)
            tx.expect(record)
            await apply_batch(collections.scores, tx, day, batch, question.answer)
            tx.put(collections.rounds, record.key, changed, status="closed")
            if finished:
                self._schedule_render(tx, record)
                self._schedule_cleanup(tx, record)
            if not await context.current():
                return
            await self._commit(tx)
            if finished:
                return

    @staticmethod
    def keyboard(definition: Definition, state: RoundState, view: View) -> InlineKeyboardMarkup | None:
        builder = InlineKeyboardBuilder()
        if state.phase != "closed" and state.question is not None:
            builder.add(
                *[
                    InlineKeyboardButton(
                        text=definition.label(state.question, index), callback_data=f"{definition.name}:{state.token}:{index}"
                    )
                    for index in range(6)
                ]
            )
            builder.adjust(2)
            builder.row(InlineKeyboardButton(text="Завершить задание", callback_data=f"{definition.name}:{state.token}:finish"))
        if view.pages > 1:
            builder.row(
                *[
                    InlineKeyboardButton(text=label, callback_data=f"{definition.name}:{state.token}:page_{page}")
                    for label, page in (("‹", view.page - 1), (f"{view.page + 1}/{view.pages}", view.page), ("›", view.page + 1))
                    if 0 <= page < view.pages
                ]
            )
        return builder.as_markup() if builder.export() else None

    async def _edit(self, method: TelegramMethod[Message | bool], presentation: Presentation) -> bool:
        try:
            await self._send(method)
            return True
        except TelegramBadRequest as error:
            if error.message.removeprefix("Bad Request: ").casefold().startswith("message is not modified"):
                return True
            return False
        except TelegramForbiddenError:
            return False
        finally:
            presentation.last_edit = asyncio.get_running_loop().time()

    async def _render(self, context: JobContext) -> None:
        job, definitions = context.job, DEFINITIONS[context.job.feature]
        record = await self.collections[job.feature].rounds.get(job.scope, job.record.key)
        if record is None or record.value.message_id is None or record.value.phase not in {"active", "closed"}:
            return
        if record.value.closed_at is not None and self.clock() >= record.value.closed_at + RESULT_TTL:
            return
        identity = job.feature, record.value.chat_id, record.key
        presentation = self._presentations.setdefault(identity, Presentation())
        await asyncio.sleep(max(0, presentation.last_edit + EDIT_INTERVAL - asyncio.get_running_loop().time()))
        # Reload after pacing: a vote burst should publish the newest state once.
        record = await self.collections[job.feature].rounds.get(job.scope, job.record.key)
        if record is None or record.value.message_id is None:
            return
        votes = await self.votes(job.feature, job.scope, record.key)
        state = record.value
        view = definitions.render(state, [vote.value for vote in votes])
        markup = self.keyboard(definitions, state, view)
        needs_photo = job.feature == "chess" and state.phase == "closed" and not presentation.solution_shown
        if view == presentation.view and markup == presentation.markup and not needs_photo:
            return
        if not await context.current():
            return
        delivered = False
        photo_retry = False
        if needs_photo and state.question is not None:
            try:
                async with asyncio.timeout(5):
                    photo = await definitions.photo(state.question, solution=True)
                if not await context.current():
                    return
                delivered = await self._edit(
                    EditMessageMedia(
                        chat_id=state.chat_id,
                        message_id=state.message_id,
                        media=InputMediaPhoto(media=photo, caption=view.caption, caption_entities=view.entities, parse_mode=None),
                        reply_markup=markup,
                    ),
                    presentation,
                )
                presentation.solution_shown = delivered
            except TimeoutError, TelegramAPIError:
                photo_retry = True
                logger.warning("Quiz solution photo could not be updated")
            except ValueError, OSError:
                logger.warning("Quiz solution photo could not be updated")
        if not delivered:
            if not await context.current():
                return
            if needs_photo:
                await asyncio.sleep(max(0, presentation.last_edit + EDIT_INTERVAL - asyncio.get_running_loop().time()))
            delivered = await self._edit(
                EditMessageCaption(
                    chat_id=state.chat_id,
                    message_id=state.message_id,
                    caption=view.caption,
                    caption_entities=view.entities,
                    parse_mode=None,
                    reply_markup=markup,
                ),
                presentation,
            )
        if not delivered:
            raise JobHold("Quiz message can no longer be edited")
        presentation.view, presentation.markup = view, markup
        # A text fallback is useful now; retrying the original job can still repair the board.
        if needs_photo and not presentation.solution_shown:
            if photo_retry:
                raise JobRetry("Quiz solution photo can safely be retried")
            raise JobHold("Quiz solution photo is awaiting repair")

    async def _cleanup(self, context: JobContext) -> None:
        job, collections = context.job, self.collections[context.job.feature]
        record = await collections.rounds.get(job.scope, job.record.key)
        if record is None:
            return
        if record.value.phase not in {"closed", "abandoned"} or record.value.score_status == "pending":
            raise JobHold("Quiz cleanup awaits settlement")
        if record.value.closed_at is not None and record.value.phase == "closed" and self.clock() < record.value.closed_at + RESULT_TTL:
            raise JobRetry("Quiz result window is still open")
        while await context.current():
            record = await collections.rounds.get(job.scope, job.record.key)
            if record is None:
                return
            votes = await collections.votes.list(job.scope, parent=record.key, limit=50)
            tx = self._tx(job.feature, job.scope)
            tx.expect(record)
            for vote in votes:
                tx.delete(vote)
            if not votes:
                tx.cancel_job(f"render:{record.key}")
                tx.cancel_job(f"deadline:{record.key}")
                tx.cancel_job(f"settle:{record.key}")
                tx.cancel_job(f"cleanup:{record.key}")
                tx.delete(record)
            await self._commit(tx)
            if not votes:
                self._presentations.pop((job.feature, record.value.chat_id, record.key), None)
                return
