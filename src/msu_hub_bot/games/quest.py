"""Durable cooperative story choices; clocks start with the first accepted vote."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramNotFound
from aiogram.methods import EditMessageCaption, EditMessageMedia, EditMessageText, SendPhoto, TelegramMethod
from aiogram.types import BufferedInputFile, CallbackQuery, InputMediaPhoto, Message
from pydantic import AwareDatetime, Field, JsonValue

from msu_hub_bot.commands.quest_view import QuestCallback, QuestView, render
from msu_hub_bot.providers.quest import QuestError as ProviderQuestError
from msu_hub_bot.providers.quest import QuestProvider, QuestScene
from msu_hub_bot.storage.features import (
    Conflict,
    FeatureProtocolError,
    FeatureStore,
    FeatureWorker,
    JobContext,
    JobHold,
    JobRetry,
    Payload,
    Record,
    RecordKey,
    Scope,
    Transaction,
)
from msu_hub_bot.storage.supabase import RepositoryUnavailable

FEATURE = "quest"
CHOICE_TIME = timedelta(minutes=10)
PUBLICATION_WINDOW = timedelta(minutes=1)
RESULT_WINDOW = timedelta(days=1)
MAX_VOTERS = 10_000
SEND_TIMEOUT = 15
logger = logging.getLogger(__name__)


class QuestError(ValueError):
    """Safe user-facing explanation of a rejected action."""


class QuestChat(Payload):
    active: str | None = None


class QuestVote(Payload):
    user_id: int = Field(strict=True, gt=0)
    step: int = Field(strict=True, ge=0)
    choice: int = Field(strict=True, ge=0)
    label: str = Field(max_length=320)


class SavedQuest(Payload):
    token: str = Field(pattern=r"^[a-f0-9]{12}$")
    bot_id: int = Field(strict=True, gt=0)
    chat_id: int = Field(strict=True)
    thread_id: int | None = None
    source_message_id: int = Field(strict=True, gt=0)
    message_id: int | None = None
    created_at: AwareDatetime
    publication_due: AwareDatetime
    status: Literal["publishing", "active", "advancing", "finished", "abandoned"] = "publishing"
    story_id: str
    digest: str
    title: str
    author: str
    state: dict[str, JsonValue]
    text: str
    choices: list[str]
    counts: list[int]
    step: int = 0
    voters: int = 0
    deadline: AwareDatetime | None = None
    selected: int | None = None
    last_choice: str | None = None
    page: int = 0
    finished_at: AwareDatetime | None = None
    image_available: bool = False
    photo_message_id: int | None = None
    photo_attempted_step: int | None = None
    presentation_failed: bool = False
    advance_attempts: int = 0


class QuestService:
    def __init__(
        self,
        bot: Bot,
        store: FeatureStore,
        worker: FeatureWorker,
        provider: QuestProvider,
        *,
        clock: Callable[[], datetime] | None = None,
        choose: Callable[[Sequence[int]], int] | None = None,
    ) -> None:
        self.bot, self.store, self.worker, self.provider = bot, store, worker, provider
        self.clock = clock or (lambda: datetime.now(UTC))
        self.choose = choose or secrets.choice
        self.chats = store.collection(FEATURE, "chats", QuestChat, retention=timedelta(days=30))
        self.games = store.collection(FEATURE, "games", SavedQuest, retention=None)
        self.votes = store.collection(FEATURE, "votes", QuestVote, retention=None)
        # These locks only prevent obsolete local renders; all game facts use CAS.
        self._renders = [asyncio.Lock() for _ in range(64)]
        for kind, handler in (
            ("recover", self._recover),
            ("deadline", self._deadline),
            ("advance", self._advance),
            ("render", self._render),
            ("image", self._image),
            ("cleanup", self._cleanup),
        ):
            worker.register(FEATURE, kind, self._retryable(handler), max_attempts=1024)

    @staticmethod
    def _retryable(handler: Callable[[JobContext], Awaitable[None]]) -> Callable[[JobContext], Awaitable[None]]:
        async def execute(context: JobContext) -> None:
            for _ in range(4):
                try:
                    await handler(context)
                    return
                except Conflict:
                    if not await context.current():
                        return
                except RepositoryUnavailable, TelegramAPIError, TimeoutError:
                    if await context.current():
                        raise JobRetry("Conditional quest work can safely be replayed") from None
                    return
            raise JobRetry("Quest state changed concurrently")

        return execute

    @staticmethod
    def scope(chat_id: int) -> Scope:
        return Scope(f"chat:{chat_id}")

    @staticmethod
    def token(bot_id: int, chat_id: int, message_id: int) -> str:
        return hashlib.blake2s(f"{bot_id}:{chat_id}:{message_id}".encode(), digest_size=6).hexdigest()

    def _tx(self, scope: Scope) -> Transaction:
        return self.store.transaction(FEATURE, scope, operation_id=uuid4().hex)

    @staticmethod
    async def _commit(tx: Transaction) -> None:
        try:
            await tx.commit()
        except RepositoryUnavailable, TimeoutError:
            await tx.commit()

    async def _send[T](self, method: TelegramMethod[T]) -> T:
        async with asyncio.timeout(SEND_TIMEOUT):
            return await self.bot(method, request_timeout=SEND_TIMEOUT)

    async def get(self, chat_id: int, token: str) -> Record[SavedQuest] | None:
        if not re.fullmatch(r"[a-f0-9]{12}", token):
            raise QuestError("Этот квест уже недоступен.")
        row = await self.games.get(self.scope(chat_id), token)
        if row is not None and (row.value.bot_id, row.value.chat_id, row.value.token) != (self.bot.id, chat_id, token):
            raise FeatureProtocolError()
        return row

    async def _job(self, context: JobContext) -> Record[SavedQuest] | None:
        if context.job.record.collection != "games":
            raise JobHold("Invalid quest job identity")
        row = await self.games.get(context.job.scope, context.job.record.key)
        if row is not None and (
            row.value.bot_id != self.bot.id or row.key != row.value.token or row.scope != self.scope(row.value.chat_id)
        ):
            raise JobHold("Invalid quest record identity")
        return row

    def _schedule(self, tx: Transaction, key: str, kind: str, when: datetime) -> None:
        tx.schedule(f"{kind}:{key}", kind, record=RecordKey("games", key), run_at=when)

    async def _put(self, row: Record[SavedQuest], value: SavedQuest, tx: Transaction | None = None) -> None:
        tx = tx or self._tx(row.scope)
        tx.expect(row)
        if value.status in {"finished", "abandoned"}:
            value.finished_at = value.finished_at or self.clock()
            chat = await self.chats.get(row.scope, "active")
            if chat is not None and chat.value.active == row.key:
                tx.expect(chat)
                tx.put(self.chats, chat.key, chat.value.model_copy(update={"active": None}))
            tx.cancel_job(f"deadline:{row.key}")
            tx.cancel_job(f"recover:{row.key}")
            self._schedule(tx, row.key, "cleanup", value.finished_at + RESULT_WINDOW)
        tx.put(self.games, row.key, value, status=value.status)
        if value.message_id is not None and value.status != "abandoned":
            self._schedule(tx, row.key, "render", self.clock())
        await self._commit(tx)

    async def _view(self, row: Record[SavedQuest]) -> QuestView:
        value = row.value
        entries = await self.votes.list(row.scope, parent=f"{row.key}:{value.step}", limit=200)
        labels = [entry.value.label for entry in entries]
        if value.voters > len(labels):
            labels.append(f"И ещё {value.voters - len(labels)} участников")
        scene = QuestScene(text=value.text, choices=tuple(value.choices), image=None)
        if value.status == "advancing":
            scene = QuestScene(text=f"Выбор завершён: {value.last_choice}\n\nЗагружаю следующую сцену…", choices=(), image=None)
        return render(
            scene,
            title=value.title,
            author=value.author,
            token=row.key,
            step=value.step,
            counts=value.counts if value.status != "advancing" else [],
            voters=labels,
            deadline=value.deadline,
            finished=value.status == "finished",
            last_choice=value.last_choice,
            page=value.page,
        )

    async def start(self, message: Message, story_id: str) -> Message | None:
        if message.from_user is None or message.from_user.is_bot or message.sender_chat is not None:
            return await self._send(message.reply("Начни квест от своего имени."))
        scope = self.scope(message.chat.id)
        token = self.token(self.bot.id, message.chat.id, message.message_id)
        if await self.get(message.chat.id, token) is not None:
            return await self._send(message.reply("Этот квест уже создан. Используй его кнопки."))
        pointer = await self.chats.get(scope, "active")
        if pointer is not None and pointer.value.active is not None:
            outcome = await self._probe_active(message.chat.id, pointer.value.active)
            if outcome == "released":
                return await self._send(message.reply("Карточка прошлого квеста недоступна. Теперь можно начать новый /quest."))
            if outcome == "unconfirmed":
                return await self._send(message.reply("Не удалось проверить карточку текущего квеста. Попробуй /quest ещё раз чуть позже."))
            return await self._send(message.reply("Подождите, текущий квест ещё не завершён!"))
        async with asyncio.timeout(25):
            book = await self.provider.load(story_id)
            state = book.start()
            scene = book.view(state)
        now = self.clock()
        value = SavedQuest(
            token=token,
            bot_id=self.bot.id,
            chat_id=message.chat.id,
            thread_id=message.message_thread_id if message.is_topic_message else None,
            source_message_id=message.message_id,
            created_at=now,
            publication_due=now + PUBLICATION_WINDOW,
            story_id=book.id,
            digest=book.digest,
            title=book.title,
            author=book.author,
            state=state,
            text=scene.text,
            choices=list(scene.choices),
            counts=[0] * len(scene.choices),
            image_available=scene.image is not None,
        )
        tx = self._tx(scope)
        if pointer is None:
            tx.expect_absent("chats", "active")
        else:
            tx.expect(pointer)
        tx.expect_absent("games", token)
        tx.put(self.chats, "active", QuestChat(active=token), expires_at=None)
        tx.put(self.games, token, value, status="publishing")
        self._schedule(tx, token, "recover", value.publication_due)
        try:
            await self._commit(tx)
        except Conflict:
            return await self._send(message.reply("Подождите, текущий квест ещё не завершён!"))
        row = await self.get(message.chat.id, token)
        assert row is not None
        sending = False
        try:
            view = await self._view(row)
            sending = True
            sent = await self._send(message.reply(view.text, parse_mode=None, reply_markup=view.keyboard))
            await self._bind(row, sent)
            return sent
        except TelegramBadRequest, TelegramForbiddenError:
            await self._abandon(row)
            return await self._send(message.reply("Не удалось показать квест. Попробуй ещё раз."))
        except asyncio.CancelledError:
            if not sending:
                await asyncio.shield(self._abandon(row))
            raise
        except Exception:
            if not sending:
                await self._abandon(row)
            logger.warning("Quest publication could not be confirmed")
            return await self._send(
                message.reply("Не удалось подтвердить отправку. Если квест появился, нажми его кнопку; иначе попробуй через минуту.")
            )

    async def _probe_active(self, chat_id: int, token: str) -> Literal["active", "released", "unconfirmed"]:
        # A scene without votes intentionally has no timer or periodic edits.
        # Repeating /quest also checks whether its saved controls still exist.
        async with self._renders[hash(token) % len(self._renders)]:
            for _ in range(4):
                row = await self.get(chat_id, token)
                if row is None or row.value.status in {"finished", "abandoned"}:
                    pointer = await self.chats.get(self.scope(chat_id), "active")
                    if pointer is None or pointer.value.active != token:
                        return "released"
                    tx = self._tx(pointer.scope)
                    tx.expect(pointer)
                    if row is None:
                        tx.expect_absent("games", token)
                    else:
                        tx.expect(row)
                    tx.put(self.chats, pointer.key, pointer.value.model_copy(update={"active": None}))
                    try:
                        await self._commit(tx)
                        return "released"
                    except Conflict:
                        continue
                if row.value.message_id is None:
                    if row.value.status == "publishing" and self.clock() >= row.value.publication_due:
                        try:
                            await self._abandon(row)
                            return "released"
                        except Conflict:
                            continue
                    return "active"
                view = await self._view(row)
                try:
                    await self._edit_text(row, view)
                    return "active"
                except TelegramBadRequest as error:
                    if "message is not modified" in error.message.casefold():
                        return "active"
                    if not any(reason in error.message.casefold() for reason in ("message to edit not found", "message can't be edited")):
                        return "unconfirmed"
                except TelegramForbiddenError, TelegramNotFound:
                    pass
                except TelegramAPIError, TimeoutError:
                    # A failed network response cannot prove the card is gone.
                    return "unconfirmed"
                try:
                    await self._presentation_failed(row)
                    return "released"
                except Conflict:
                    continue
        return "unconfirmed"

    async def _edit_text(self, row: Record[SavedQuest], view: QuestView) -> None:
        await self._send(
            EditMessageText(
                chat_id=row.value.chat_id,
                message_id=row.value.message_id,
                text=view.text,
                parse_mode=None,
                reply_markup=view.keyboard,
            )
        )

    async def _abandon(self, row: Record[SavedQuest]) -> None:
        current = await self.get(row.value.chat_id, row.key)
        if current is not None and current.value.status == "publishing":
            value = current.value.model_copy(deep=True)
            value.status = "abandoned"
            await self._put(current, value)

    async def _bind(self, row: Record[SavedQuest], message: Message) -> None:
        for _ in range(8):
            current = await self.get(row.value.chat_id, row.key)
            if current is None:
                raise QuestError("Этот квест уже недоступен.")
            if current.value.message_id is not None:
                if current.value.message_id != message.message_id:
                    raise QuestError("Это другая карточка квеста.")
                return
            if current.value.status != "publishing" or self.clock() >= current.value.publication_due:
                raise QuestError("Этот квест уже недоступен.")
            value = current.value.model_copy(deep=True)
            value.message_id = message.message_id
            value.status = "active" if value.choices else "finished"
            tx = self._tx(row.scope)
            if value.status == "active":
                tx.cancel_job(f"recover:{row.key}")
            if value.image_available:
                self._schedule(tx, row.key, "image", self.clock())
            try:
                await self._put(current, value, tx)
                return
            except Conflict:
                continue
        raise Conflict()

    async def _matches(self, row: Record[SavedQuest], message: Message) -> bool:
        value = row.value
        if (
            message.chat.id != value.chat_id
            or (message.is_topic_message and message.message_thread_id != value.thread_id)
            or message.from_user is None
            or message.from_user.id != self.bot.id
            or not message.from_user.is_bot
            or message.forward_origin is not None
        ):
            return False
        if value.message_id is not None:
            return value.message_id == message.message_id
        view = await self._view(row)
        return (
            value.status == "publishing"
            and self.clock() < value.publication_due
            and message.reply_to_message is not None
            and message.reply_to_message.message_id == value.source_message_id
            and message.text == view.text
            and message.reply_markup is not None
            and view.keyboard is not None
            and message.reply_markup.model_dump(mode="json") == view.keyboard.model_dump(mode="json")
        )

    async def _vote(self, row: Record[SavedQuest], user_id: int, label: str, choice: int) -> None:
        value = row.value.model_copy(deep=True)
        if not 0 <= choice < len(value.choices):
            raise QuestError("Такого варианта нет.")
        key = f"{row.key}:{value.step}:{user_id}"
        old = await self.votes.get(row.scope, key)
        tx = self._tx(row.scope)
        if old is None:
            if value.voters >= MAX_VOTERS:
                raise QuestError("В этом выборе уже слишком много участников.")
            tx.expect_absent("votes", key)
            value.voters += 1
        else:
            if old.value.step != value.step or old.value.user_id != user_id or not 0 <= old.value.choice < len(value.counts):
                raise FeatureProtocolError()
            tx.expect(old)
            value.counts[old.value.choice] -= 1
        value.counts[choice] += 1
        if value.deadline is None:
            value.deadline = self.clock() + CHOICE_TIME
            self._schedule(tx, row.key, "deadline", value.deadline)
        tx.put(self.votes, key, QuestVote(user_id=user_id, step=value.step, choice=choice, label=label), parent=f"{row.key}:{value.step}")
        await self._put(row, value, tx)

    async def _finish(self, row: Record[SavedQuest]) -> None:
        value = row.value.model_copy(deep=True)
        if value.status != "active":
            return
        if not value.voters:
            raise QuestError("Сначала должен проголосовать хотя бы один участник.")
        high = max(value.counts)
        leaders = [index for index, count in enumerate(value.counts) if count == high]
        value.selected = self.choose(leaders)
        if value.selected not in leaders:
            raise FeatureProtocolError()
        value.last_choice = value.choices[value.selected]
        value.status = "advancing"
        value.deadline = None
        tx = self._tx(row.scope)
        tx.cancel_job(f"deadline:{row.key}")
        self._schedule(tx, row.key, "advance", self.clock())
        await self._put(row, value, tx)

    async def callback(self, query: CallbackQuery, callback_data: QuestCallback) -> None:
        answer = ""
        try:
            if not isinstance(query.message, Message) or query.from_user.is_bot:
                raise QuestError("Квест недоступен.")
            for _ in range(8):
                row = await self.get(query.message.chat.id, callback_data.game_id)
                if row is None or not await self._matches(row, query.message):
                    raise QuestError("Этот квест уже недоступен.")
                try:
                    if row.value.status == "publishing":
                        await self._bind(row, query.message)
                        continue
                    if callback_data.scene_version != row.value.step:
                        raise QuestError("Сцена уже изменилась. Используй новые кнопки.")
                    if callback_data.action == "page":
                        page = int(callback_data.value)
                        if not 0 <= page <= 1000:
                            raise QuestError("Эта страница недоступна.")
                        value = row.value.model_copy(update={"page": page})
                        await self._put(row, value)
                        break
                    if row.value.status != "active":
                        raise QuestError("Выбор уже завершён.")
                    if row.value.deadline is not None and self.clock() >= row.value.deadline:
                        await self._finish(row)
                        raise QuestError("Время выбора истекло.")
                    if callback_data.action == "vote":
                        user = query.from_user
                        label = user.full_name[:256] + (f" (@{user.username})" if user.username else "")
                        await self._vote(row, user.id, label, int(callback_data.value))
                        answer = "Голос сохранён. До завершения можно выбрать другой вариант."
                    elif callback_data.action == "finish":
                        await self._finish(row)
                        answer = "Выбор завершён."
                    else:
                        raise QuestError("Эта кнопка недоступна.")
                    break
                except Conflict:
                    continue
            else:
                raise QuestError("Выбор обновился. Попробуй ещё раз.")
        except QuestError as error:
            answer = str(error)
        except ValueError:
            answer = "Эта кнопка недоступна."
        except Exception:
            logger.warning("Quest action could not be confirmed")
            answer = "Не удалось подтвердить действие. Проверь квест и попробуй ещё раз."
        try:
            await self._send(query.answer(answer))
        except TelegramAPIError, TimeoutError:
            pass

    async def _recover(self, context: JobContext) -> None:
        row = await self._job(context)
        if row is None or row.value.status != "publishing" or not await context.current():
            return
        if self.clock() < row.value.publication_due:
            raise JobRetry("Quest publication is still pending")
        await self._abandon(row)

    async def _deadline(self, context: JobContext) -> None:
        row = await self._job(context)
        if row is None or row.value.status != "active" or row.value.deadline is None or not await context.current():
            return
        if self.clock() < row.value.deadline:
            tx = self._tx(row.scope)
            tx.expect(row)
            self._schedule(tx, row.key, "deadline", row.value.deadline)
            await self._commit(tx)
            return
        await self._finish(row)

    async def _advance(self, context: JobContext) -> None:
        row = await self._job(context)
        if row is None or row.value.status != "advancing" or row.value.selected is None:
            return
        try:
            async with asyncio.timeout(25):
                book = await self.provider.load(row.value.story_id, expected_hash=row.value.digest)
                state = book.choose(row.value.state, row.value.selected)
                scene = book.view(state)
        except ProviderQuestError, TimeoutError:
            if await context.current():
                await self._advance_failed(row)
            return
        value = row.value.model_copy(deep=True)
        value.advance_attempts = 0
        value.state, value.text, value.choices = state, scene.text, list(scene.choices)
        value.counts, value.voters, value.deadline = [0] * len(scene.choices), 0, None
        value.step += 1
        value.page, value.selected = 0, None
        value.image_available = scene.image is not None
        value.status = "active" if scene.choices else "finished"
        tx = self._tx(row.scope)
        if value.image_available or value.photo_message_id is not None:
            self._schedule(tx, row.key, "image", self.clock())
        else:
            tx.cancel_job(f"image:{row.key}")
        if not await context.current():
            return
        await self._put(row, value, tx)

    async def _advance_failed(self, row: Record[SavedQuest]) -> None:
        value = row.value.model_copy(deep=True)
        value.advance_attempts += 1
        tx = self._tx(row.scope)
        if value.advance_attempts < 3:
            self._schedule(tx, row.key, "advance", self.clock() + timedelta(seconds=15))
        else:
            value.status = "finished"
            value.text = "Квест остановлен: не удалось загрузить продолжение истории. Можно начать заново командой /quest."
            value.choices, value.counts = [], []
            value.image_available = False
            tx.cancel_job(f"image:{row.key}")
        await self._put(row, value, tx)

    async def _render(self, context: JobContext) -> None:
        async with self._renders[hash(context.job.record.key) % len(self._renders)]:
            row = await self._job(context)
            if row is None or row.value.message_id is None or row.value.status == "abandoned":
                return
            view = await self._view(row)
            if not await context.current():
                return
            try:
                await self._edit_text(row, view)
            except TelegramBadRequest as error:
                if "message is not modified" in error.message.casefold():
                    return
                if not any(reason in error.message.casefold() for reason in ("message to edit not found", "message can't be edited")):
                    raise JobHold("Quest text was rejected") from None
                await self._presentation_failed(row)
            except TelegramForbiddenError, TelegramNotFound:
                await self._presentation_failed(row)

    async def _presentation_failed(self, row: Record[SavedQuest]) -> None:
        # Without the bound controls a waiting scene cannot receive its first
        # vote. Release the chat instead of leaving an unreachable active quest.
        value = row.value.model_copy(deep=True)
        value.presentation_failed = True
        tx = self._tx(row.scope)
        if value.status not in {"finished", "abandoned"}:
            value.status = "abandoned"
            tx.cancel_job(f"advance:{row.key}")
            tx.cancel_job(f"image:{row.key}")
            await self._put(row, value, tx)
        else:
            tx.expect(row)
            tx.put(self.games, row.key, value, status=value.status)
            await self._commit(tx)

    async def _image(self, context: JobContext) -> None:
        row = await self._job(context)
        if row is None or row.value.message_id is None or row.value.status in {"publishing", "advancing", "abandoned"}:
            return
        value = row.value
        if not value.image_available:
            if value.photo_message_id is not None and await context.current():
                try:
                    await self._send(
                        EditMessageCaption(
                            chat_id=value.chat_id,
                            message_id=value.photo_message_id,
                            caption=f"{value.title[:600]}\nИллюстрация предыдущей сцены. У текущей сцены своей картинки нет.",
                            parse_mode=None,
                        )
                    )
                except TelegramBadRequest, TelegramForbiddenError, TelegramNotFound:
                    pass
            return
        if value.photo_message_id is None and value.photo_attempted_step is not None:
            # A lost send response is not evidence that Telegram did not show the photo.
            return
        try:
            async with asyncio.timeout(25):
                book = await self.provider.load(value.story_id, expected_hash=value.digest)
                image = book.view(value.state).image
        except ProviderQuestError:
            # A missing illustration does not invalidate the saved story text.
            return
        if image is None or not await context.current():
            return
        if value.photo_message_id is not None:
            try:
                await self._send(
                    EditMessageMedia(
                        chat_id=value.chat_id,
                        message_id=value.photo_message_id,
                        media=InputMediaPhoto(
                            media=BufferedInputFile(image, filename="quest.png"), caption=value.title[:800], parse_mode=None
                        ),
                    )
                )
            except TelegramBadRequest as error:
                if "message is not modified" not in error.message.casefold():
                    raise JobHold("Quest illustration cannot be updated") from None
            except TelegramForbiddenError, TelegramNotFound:
                raise JobHold("Quest illustration is unavailable") from None
            return
        tx = self._tx(row.scope)
        tx.expect(row)
        tx.put(self.games, row.key, value.model_copy(update={"photo_attempted_step": value.step}), status=value.status)
        await self._commit(tx)
        if not await context.current():
            await self._clear_unsent_image(row)
            return
        # The durable marker is committed before this one automatic send attempt.
        try:
            sent = await self._send(
                SendPhoto(
                    chat_id=value.chat_id,
                    message_thread_id=value.thread_id,
                    photo=BufferedInputFile(image, filename="quest.png"),
                    caption=value.title[:800],
                    parse_mode=None,
                )
            )
        except TelegramAPIError, TimeoutError:
            return
        for _ in range(8):
            current = await self.get(value.chat_id, row.key)
            if current is None or current.value.status == "abandoned":
                return
            changed = current.value.model_copy(update={"photo_message_id": sent.message_id})
            tx = self._tx(row.scope)
            tx.expect(current)
            tx.put(self.games, row.key, changed, status=changed.status)
            if changed.step != value.step and changed.image_available:
                self._schedule(tx, row.key, "image", self.clock())
            try:
                await self._commit(tx)
                return
            except Conflict:
                continue
        raise Conflict()

    async def _clear_unsent_image(self, previous: Record[SavedQuest]) -> None:
        # This path is reachable only before SendPhoto was called. An ambiguous
        # response after that call deliberately retains the publication marker.
        for _ in range(8):
            row = await self.get(previous.value.chat_id, previous.key)
            if row is None or row.value.photo_message_id is not None or row.value.photo_attempted_step != previous.value.step:
                return
            value = row.value.model_copy(update={"photo_attempted_step": None})
            tx = self._tx(row.scope)
            tx.expect(row)
            tx.put(self.games, row.key, value, status=value.status)
            if value.image_available and value.status in {"active", "finished"}:
                self._schedule(tx, row.key, "image", self.clock())
            try:
                await self._commit(tx)
                return
            except Conflict:
                continue
        raise Conflict()

    async def _cleanup(self, context: JobContext) -> None:
        row = await self._job(context)
        if row is None:
            return
        if row.value.status not in {"finished", "abandoned"} or row.value.finished_at is None:
            raise JobHold("Active quest cannot be removed")
        if self.clock() < row.value.finished_at + RESULT_WINDOW:
            raise JobRetry("Quest result is still available")
        # Prefix keys keep every step's votes together without growing the game record.
        rows = await self.votes.list(row.scope, after=f"{row.key}:", limit=50)
        votes = [entry for entry in rows if entry.key.startswith(f"{row.key}:")]
        tx = self._tx(row.scope)
        tx.expect(row)
        for vote in votes:
            tx.expect(vote)
            tx.delete(vote)
        if votes:
            self._schedule(tx, row.key, "cleanup", self.clock())
        else:
            for kind in ("render", "image", "advance", "deadline", "recover"):
                tx.cancel_job(f"{kind}:{row.key}")
            tx.delete(row)
        if await context.current():
            await self._commit(tx)
