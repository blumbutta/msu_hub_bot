import asyncio
import logging
import random
import secrets
import time as wall_time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, TypeVar, cast
from collections.abc import Awaitable

from cachetools import LRUCache, TTLCache
from pydantic import TypeAdapter
from aiogram import Bot

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.methods import TelegramMethod
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.utils.formatting import Text
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.runtime import AdmissionClosed, Supervisor
from msu_hub_bot.telegram.storage import RedisStorage
from msu_hub_bot.telegram import quiz_store

from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.geoguess import COUNTRIES, Photo, random_photo
from msu_hub_bot.commands.geoguess_view import Player, country_label, render
from msu_hub_bot.commands.quiz_view import View, user_label

logger = logging.getLogger(__name__)
SEND_TIMEOUT = 15
PHOTO_TIMEOUT = 10
ROUND_TIMEOUT = 10 * 60
DAY_ZONE = ZoneInfo("Europe/Moscow")
EDIT_INTERVAL = 1.0
RESULT_TTL = 24 * 60 * 60
MAX_ROUNDS = 128
RETRY_INTERVAL = 30.0
FINISH_TIMEOUT = 45.0
PHOTO_ADAPTER = TypeAdapter(Photo)
_Result = TypeVar("_Result")


async def _send(method: TelegramMethod[_Result]) -> _Result:
    # Include middleware waits and retries in the operation's deadline.
    async with asyncio.timeout(SEND_TIMEOUT):
        return await bot_for(method)(method, request_timeout=SEND_TIMEOUT)


@dataclass
class Round:
    token: str
    photo: Optional[Photo] = None
    options: list[str] = field(default_factory=list)
    message: Optional[Message] = None
    votes: dict[int, tuple[int, str]] = field(default_factory=dict)
    usernames: dict[int, str | None] = field(default_factory=dict)
    task: Optional[asyncio.Task[None]] = None
    timer: asyncio.TimerHandle | None = None
    closed: bool = False
    scored: bool | None = None
    page: int = 0
    view: View | None = None
    markup: InlineKeyboardMarkup | None = None
    last_edit: float = 0.0
    board_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    deadline: float = 0.0
    score_day: date | None = None
    revealed: bool = False
    needs_save: bool = False


def today() -> date:
    return datetime.now(DAY_ZONE).date()


def score_key(chat_id: int, day: date | None = None) -> str:
    return f"msu_hub:geoguess:{chat_id}:{(day or today()).isoformat()}:scores"


# Apply a round once, even if Redis retries after losing the response.
_SAVE_SCORES = """
if redis.call('SISMEMBER', KEYS[4], ARGV[2]) == 1 then
    return 0
end
for i = 3, #ARGV, 4 do
    local uid = ARGV[i]
    local score = tonumber(redis.call('ZSCORE', KEYS[1], uid) or '0')
    redis.call('ZADD', KEYS[1], math.max(0, score + tonumber(ARGV[i + 3])), uid)
    redis.call('HSET', KEYS[2], uid, ARGV[i + 1])
    redis.call('HSET', KEYS[3], uid, ARGV[i + 2])
end
redis.call('SADD', KEYS[4], ARGV[2])
for _, key in ipairs(KEYS) do
    redis.call('EXPIREAT', key, ARGV[1])
end
return 1
"""


async def save_scores(
    chat_id: int, players: list[tuple[int, str, str | None, int]], redis: RedisStorage, day: date | None = None, *, round_token: str
) -> None:
    if not players:
        return
    day = day or today()
    key = score_key(chat_id, day)
    # Brief retention for a round closing at midnight; reads always use today's key.
    expires = int(datetime.combine(day + timedelta(days=2), time.min, DAY_ZONE).timestamp())
    args: list[str | int] = [expires, round_token]
    for user_id, name, username, delta in players:
        args.extend((str(user_id), name, username or "", delta))
    client = await redis.redis()
    await cast(Awaitable[int], client.eval(_SAVE_SCORES, 4, key, key + ":names", key + ":usernames", key + ":rounds", *args))


class GeoguessCallback(CallbackData, prefix="geoguess"):
    round: str
    choice: str


class Geoguess:
    callback_data = GeoguessCallback
    rounds: dict[int, Round] = {}
    recent_countries: LRUCache[int, tuple[str, ...]] = LRUCache(maxsize=1024)
    completed: TTLCache[tuple[int, str], Round] = TTLCache(maxsize=128, ttl=RESULT_TTL)

    @classmethod
    async def process(cls, message: Message, redis: RedisStorage, supervisor: Supervisor) -> Message | None:
        chat_id = message.chat.id
        if chat_id in cls.rounds:
            return await _send(message.reply("Подождите, прошлое задание еще не окончено!"))
        if len(cls.rounds) >= MAX_ROUNDS:
            return await _send(message.reply("Сейчас слишком много игр. Попробуй немного позже."))
        round_ = Round(secrets.token_hex(6))
        cls.rounds[chat_id] = round_
        started = False
        try:
            await asyncio.wait_for(cls.send_round_photo(message, round_), timeout=PHOTO_TIMEOUT)
            started = True
            round_.deadline = wall_time.time() + ROUND_TIMEOUT
            # Arm first: a slow/unavailable state store must not lose the live timer.
            cls.arm(chat_id, round_, redis, supervisor)
            try:
                await cls.persist(round_, redis)
            except Exception:
                logger.exception("Geoguess initial round snapshot failed")
        except ExternalServiceError, TelegramAPIError, asyncio.TimeoutError:
            if round_.timer is not None:
                round_.timer.cancel()
            if cls.rounds.get(chat_id) is round_:
                cls.rounds.pop(chat_id, None)
            await _send(message.reply("Ошибка, попробуйте еще раз"))
        finally:
            if not started and cls.rounds.get(chat_id) is round_:
                cls.rounds.pop(chat_id, None)

        return None

    @classmethod
    async def send_round_photo(cls, message: Message, round_: Round) -> None:
        chat_id = message.chat.id
        recent = cls.recent_countries.get(chat_id, ())
        photo = await random_photo(recent)
        options = random.sample(sorted(set(COUNTRIES.values()) - {photo.country}), 5) + [photo.country]
        random.shuffle(options)
        round_.photo, round_.options = photo, options
        view = cls.render_view(round_)
        markup = cls.keyboard(round_, view)
        round_.message = await _send(
            message.reply_photo(photo.url, caption=view.caption, caption_entities=view.entities, parse_mode=None, reply_markup=markup)
        )
        round_.view, round_.markup = view, markup
        round_.last_edit = asyncio.get_running_loop().time()
        # Remember only delivered questions; failed starts must not affect selection.
        cls.recent_countries[chat_id] = (*recent, photo.country)[-15:]

    @staticmethod
    async def save_state(round_: Round, redis: RedisStorage) -> None:
        assert round_.message is not None and round_.photo is not None
        await quiz_store.save(
            "geoguess",
            quiz_store.SavedRound(
                token=round_.token,
                message=round_.message,
                question=PHOTO_ADAPTER.dump_json(round_.photo).decode(),
                options=round_.options,
                votes=round_.votes,
                usernames=round_.usernames,
                deadline=round_.deadline,
                closed=round_.closed,
                score_day=round_.score_day,
                scored=round_.scored,
                revealed=round_.revealed,
            ),
            redis,
        )

    @classmethod
    async def persist(cls, round_: Round, redis: RedisStorage) -> None:
        # Snapshot current state after taking the lock; older vote writes cannot
        # overwrite a newer closure, even when Redis responds out of order.
        async with round_.state_lock:
            await cls.save_state(round_, redis)

    @staticmethod
    def from_state(state: quiz_store.SavedRound) -> Round:
        photo = PHOTO_ADAPTER.validate_json(state.question)
        if (
            len(state.options) != 6
            or photo.country not in state.options
            or any(not 0 <= choice < len(state.options) for choice, _ in state.votes.values())
        ):
            raise ValueError("Invalid stored GeoGuess options")
        return Round(
            token=state.token,
            photo=photo,
            options=state.options,
            message=state.message,
            votes=state.votes,
            usernames=state.usernames,
            deadline=state.deadline,
            closed=state.closed,
            score_day=state.score_day,
            scored=state.scored,
            revealed=state.revealed,
        )

    @classmethod
    def arm(cls, chat_id: int, round_: Round, redis: RedisStorage, supervisor: Supervisor) -> None:
        if round_.timer is not None:
            round_.timer.cancel()
        delay = RETRY_INTERVAL if round_.closed else max(0, round_.deadline - wall_time.time())
        round_.timer = asyncio.get_running_loop().call_later(delay, cls.start_finish, chat_id, round_, redis, supervisor)

    @classmethod
    async def restore(cls, bot: Bot, redis: RedisStorage, supervisor: Supervisor) -> None:
        for state in sorted(await quiz_store.pending("geoguess", bot, redis), key=lambda saved: saved.deadline, reverse=True):
            chat_id = state.message.chat.id
            existing = cls.rounds.get(chat_id)
            if (existing is not None and existing.token == state.token) or (chat_id, state.token) in cls.completed:
                continue
            try:
                round_ = cls.from_state(state)
            except ValueError:
                logger.warning("Invalid saved GeoGuess round")
                continue
            if round_.closed:
                cls.completed[chat_id, round_.token] = round_
                cls.start_finish(chat_id, round_, redis, supervisor)
            elif existing is None:
                cls.rounds[chat_id] = round_
                cls.arm(chat_id, round_, redis, supervisor)
            else:
                # A newer round may have started while recovery was unavailable.
                # Finish the older message without replacing that active game.
                round_.closed = True
                round_.score_day = datetime.fromtimestamp(min(round_.deadline, wall_time.time()), DAY_ZONE).date()
                cls.completed[chat_id, round_.token] = round_
                cls.start_finish(chat_id, round_, redis, supervisor)

    @classmethod
    def start_finish(cls, chat_id: int, round_: Round, redis: RedisStorage, supervisor: Supervisor) -> asyncio.Task[None] | None:
        # Claim completion without yielding, including a simultaneous button/timer.
        if cls.rounds.get(chat_id) is not round_ and cls.completed.get((chat_id, round_.token)) is not round_:
            return None
        if round_.task is not None and not round_.task.done():
            return round_.task
        if round_.closed and round_.revealed and round_.scored is True and not round_.needs_save:
            return None
        if not round_.closed:
            if cls.rounds.get(chat_id) is not round_:
                return None
            round_.closed = True
            round_.score_day = (
                datetime.fromtimestamp(round_.deadline, DAY_ZONE).date()
                if round_.deadline and wall_time.time() >= round_.deadline
                else today()
            )
        if round_.timer is not None:
            round_.timer.cancel()
        try:
            round_.task = supervisor.create_job(lambda: cls.finish(chat_id, round_, redis, supervisor))
        except AdmissionClosed:
            # The durable deadline is recovered by the next process.
            if cls.rounds.get(chat_id) is round_:
                cls.rounds.pop(chat_id, None)
            cls.completed[chat_id, round_.token] = round_
            return None
        return round_.task

    @classmethod
    async def process_cb(
        cls, query: CallbackQuery, callback_data: GeoguessCallback, redis: RedisStorage, supervisor: Supervisor
    ) -> bool | None:
        if not isinstance(query.message, Message):
            return await _send(query.answer("Этот раунд недоступен."))
        message = query.message
        round_ = cls.rounds.get(message.chat.id)
        if round_ is None or round_.token != callback_data.round:
            round_ = cls.completed.get((message.chat.id, callback_data.round))
        if round_ is None:
            try:
                state = await quiz_store.load("geoguess", bot_for(message), message.chat.id, callback_data.round, redis)
                if state is not None:
                    active = cls.rounds.get(message.chat.id)
                    round_ = (
                        active if active is not None and active.token == state.token else cls.completed.get((message.chat.id, state.token))
                    )
                    round_ = round_ or cls.from_state(state)
                    if round_.closed:
                        cls.completed[message.chat.id, round_.token] = round_
                    elif message.chat.id not in cls.rounds:
                        cls.rounds[message.chat.id] = round_
                        cls.arm(message.chat.id, round_, redis, supervisor)
                    elif cls.rounds[message.chat.id] is not round_:
                        round_.closed = True
                        round_.score_day = datetime.fromtimestamp(min(round_.deadline, wall_time.time()), DAY_ZONE).date()
                        cls.completed[message.chat.id, round_.token] = round_
                        cls.start_finish(message.chat.id, round_, redis, supervisor)
            except Exception:
                logger.exception("Geoguess round recovery failed")
        if round_ is None or round_.message is None or round_.message.message_id != message.message_id:
            return await _send(query.answer("Раунд недоступен. Начни новый: /geoguess", show_alert=True))
        if not round_.closed and round_.deadline and wall_time.time() >= round_.deadline:
            cls.start_finish(message.chat.id, round_, redis, supervisor)
        if callback_data.choice.startswith("page_"):
            page = callback_data.choice.removeprefix("page_")
            if not page.isascii() or not page.isdecimal() or len(page) > 8:
                return await _send(query.answer("Неизвестная страница."))
            try:
                await _send(query.answer())
            finally:
                await cls.update_board(round_, page=int(page))
            return None
        if round_.closed:
            try:
                await _send(query.answer("Раунд завершён. Начни новый: /geoguess", show_alert=True))
            finally:
                # Retry score/delivery failures as well as expired visible buttons.
                busy = round_.task is not None and not round_.task.done()
                if not busy:
                    task = cls.start_finish(message.chat.id, round_, redis, supervisor)
                    if task is not None:
                        await asyncio.shield(task)
                    else:
                        await cls.update_board(round_, page=0)
            return None
        if callback_data.choice == "finish":
            task = cls.start_finish(message.chat.id, round_, redis, supervisor)
            await _send(query.answer("Задание завершено!"))
            # The supervisor owns completion even if the callback worker stops.
            if task is not None:
                await asyncio.shield(task)
            return None
        user_id = query.from_user.id
        try:
            choice = int(callback_data.choice)
            if not 0 <= choice < len(round_.options):
                raise ValueError
        except KeyError, ValueError:
            return await _send(query.answer("Неизвестный вариант."))
        answer = "Ответ принят! Результат — в конце раунда."
        alert = False
        async with round_.state_lock:
            if round_.closed or (round_.deadline and wall_time.time() >= round_.deadline):
                cls.start_finish(message.chat.id, round_, redis, supervisor)
                answer = "Задание уже завершено."
            else:
                duplicate = user_id in round_.votes
                if not duplicate:
                    round_.votes[user_id] = (choice, query.from_user.full_name)
                    round_.usernames[user_id] = query.from_user.username
                try:
                    await cls.save_state(round_, redis)
                    if duplicate:
                        answer, alert = "Твой ответ уже принят. Изменить его нельзя.", True
                except Exception:
                    # Redis may have committed before its response was lost. Keep
                    # the first choice and persist it again on a user's retry.
                    logger.exception("Geoguess vote persistence could not be confirmed")
                    answer, alert = "Не удалось подтвердить ответ. Нажми тот же вариант ещё раз.", True
        try:
            await _send(query.answer(answer, show_alert=alert))
        finally:
            await cls.update_board(round_)
        return None

    @staticmethod
    def render_view(round_: Round) -> View:
        assert round_.photo is not None
        players = [
            Player(uid, name, round_.usernames.get(uid), round_.options[choice], round_.options[choice] == round_.photo.country)
            for uid, (choice, name) in round_.votes.items()
        ]
        return render(round_.photo, players, closed=round_.closed, scored=round_.scored, page=round_.page)

    @staticmethod
    def keyboard(round_: Round, view: View) -> InlineKeyboardMarkup | None:
        keyboard = InlineKeyboardBuilder()
        if not round_.closed:
            keyboard.add(
                *[
                    InlineKeyboardButton(
                        text=country_label(country), callback_data=GeoguessCallback(round=round_.token, choice=str(i)).pack()
                    )
                    for i, country in enumerate(round_.options)
                ]
            )
            keyboard.adjust(2)
            keyboard.row(
                InlineKeyboardButton(text="Завершить задание", callback_data=GeoguessCallback(round=round_.token, choice="finish").pack())
            )
        if view.pages > 1:
            buttons = []
            for label, page in (("‹", view.page - 1), (f"{view.page + 1}/{view.pages}", view.page), ("›", view.page + 1)):
                if 0 <= page < view.pages:
                    buttons.append(
                        InlineKeyboardButton(text=label, callback_data=GeoguessCallback(round=round_.token, choice=f"page_{page}").pack())
                    )
            keyboard.row(*buttons)
        return keyboard.as_markup() if keyboard.export() else None

    @classmethod
    async def update_board(cls, round_: Round, *, page: int | None = None) -> None:
        if round_.message is None:
            return
        async with round_.board_lock:
            # The score result and reveal are published together after completion.
            if round_.closed and round_.scored is None:
                return
            if page is not None:
                round_.page = page
            view = cls.render_view(round_)
            round_.page = view.page
            markup = cls.keyboard(round_, view)
            if view == round_.view and markup == round_.markup:
                return
            # Coalesce concurrent votes and keep edits below one per second.
            await asyncio.sleep(max(0, round_.last_edit + EDIT_INTERVAL - asyncio.get_running_loop().time()))
            if round_.closed and round_.scored is None:
                return
            view = cls.render_view(round_)
            markup = cls.keyboard(round_, view)
            closed_view, scored_view = round_.closed, round_.scored
            try:
                await _send(
                    round_.message.edit_caption(caption=view.caption, caption_entities=view.entities, parse_mode=None, reply_markup=markup)
                )
            except TelegramBadRequest as error:
                if not error.message.removeprefix("Bad Request: ").casefold().startswith("message is not modified"):
                    logger.warning("Geoguess caption update failed")
                    return
            except TelegramAPIError, asyncio.TimeoutError:
                logger.warning("Geoguess caption update failed")
                return
            finally:
                round_.last_edit = asyncio.get_running_loop().time()
            round_.page, round_.view, round_.markup = view.page, view, markup
            if closed_view and round_.closed and scored_view == round_.scored and view == cls.render_view(round_):
                round_.revealed = True

    @classmethod
    async def finish(cls, chat_id: int, round_: Round, redis: RedisStorage, supervisor: Supervisor | None = None) -> None:
        round_.closed = True
        round_.score_day = round_.score_day or today()
        saved = False
        cancelled = False
        try:
            async with asyncio.timeout(FINISH_TIMEOUT):
                photo = round_.photo
                if photo is None or round_.message is None:
                    return
                if round_.scored is not True:
                    round_.revealed = False
                    try:
                        # Freeze the score date durably before the idempotent score
                        # transaction. Restarting after midnight must not score twice.
                        await cls.persist(round_, redis)
                        players = [
                            (uid, name, round_.usernames.get(uid), 1 if round_.options[choice] == photo.country else -1)
                            for uid, (choice, name) in round_.votes.items()
                        ]
                        await asyncio.wait_for(save_scores(chat_id, players, redis, round_.score_day, round_token=round_.token), timeout=5)
                        round_.scored = True
                    except Exception:
                        round_.scored = False
                        logger.exception("Geoguess score update failed")
                cls.completed[chat_id, round_.token] = round_
                if cls.rounds.get(chat_id) is round_:
                    cls.rounds.pop(chat_id, None)
                await cls.update_board(round_, page=0)
                await cls.persist(round_, redis)
                saved = True
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            logger.exception("Geoguess round completion will be retried")
        finally:
            if round_.timer is not None:
                round_.timer.cancel()
            cls.completed[chat_id, round_.token] = round_
            if cls.rounds.get(chat_id) is round_:
                cls.rounds.pop(chat_id, None)
            round_.needs_save = not saved
            if (
                not cancelled
                and supervisor is not None
                and (not saved or not round_.revealed or round_.scored is not True)
                and wall_time.time() < round_.deadline + RESULT_TTL
            ):
                cls.arm(chat_id, round_, redis, supervisor)

    @classmethod
    async def top(cls, message: Message, redis: RedisStorage) -> Message:
        day = today()
        key = score_key(message.chat.id, day)

        async def read() -> list[Text]:
            client = await redis.redis()
            scores = await cast(Awaitable[list[tuple[str, float]]], client.zrevrange(key, 0, 9, withscores=True))
            rows: list[Text] = []
            for user_id, score in scores:
                name = await cast(Awaitable[str | None], client.hget(key + ":names", user_id))
                if isinstance(name, bytes):
                    name = name.decode("utf-8", errors="replace")
                username = await cast(Awaitable[str | None], client.hget(key + ":usernames", user_id))
                if isinstance(username, bytes):
                    username = username.decode("utf-8", errors="replace")
                label = user_label(int(user_id), name or "Игрок", username)
                rows.append(Text(f"{len(rows) + 1}. ", label, f" — {int(score)}"))
            return rows

        try:
            rows = await asyncio.wait_for(read(), timeout=5)
        except Exception:
            return await _send(message.reply("Рейтинг сейчас недоступен."))
        body = Text(*[Text(row, "\n") for row in rows]) if rows else Text("Пока нет очков. Начни /geoguess")
        text, entities = Text(f"🏆 Рейтинг за сегодня, {day:%d.%m.%Y} (МСК)\n\n", body).render()
        return await _send(message.reply(text, entities=entities, parse_mode=None))

    @classmethod
    async def shutdown(cls) -> None:
        rounds = {id(round_): round_ for round_ in (*cls.rounds.values(), *cls.completed.values())}.values()
        for round_ in rounds:
            if round_.timer is not None:
                round_.timer.cancel()
        tasks = [round_.task for round_ in rounds if round_.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        cls.rounds.clear()
        cls.recent_countries.clear()
        cls.completed.clear()
