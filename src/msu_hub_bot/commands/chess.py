"""Chat chess puzzles with hidden votes and a daily, chat-local leaderboard."""

import asyncio
import logging
import secrets
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from time import time as wall_time
from typing import TypeVar, cast
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.methods import TelegramMethod
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message
from aiogram.utils.formatting import Text
from aiogram.utils.keyboard import InlineKeyboardBuilder
from cachetools import LRUCache, TTLCache
from pydantic import TypeAdapter

from msu_hub_bot.commands.chess_view import Player, render
from msu_hub_bot.commands.quiz_view import View, user_label
from msu_hub_bot.media.chessboard import render_board
from msu_hub_bot.providers.chess import MoveOption, Puzzle, random_puzzle
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.telegram import quiz_store
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.runtime import AdmissionClosed, Supervisor
from msu_hub_bot.telegram.storage import RedisStorage

logger = logging.getLogger(__name__)
SEND_TIMEOUT = 15
PHOTO_TIMEOUT = 10
ROUND_TIMEOUT = 10 * 60
EDIT_INTERVAL = 1.0
RESULT_TTL = 24 * 60 * 60
UPDATE_TIMEOUT = 40
RETRY_INTERVAL = 30
PUZZLE_ADAPTER = TypeAdapter(Puzzle)
DAY_ZONE = ZoneInfo("Europe/Moscow")
MAX_ROUNDS = 128
_Result = TypeVar("_Result")


async def _send(method: TelegramMethod[_Result]) -> _Result:
    # Middleware waits and retries belong to the same delivery deadline.
    async with asyncio.timeout(SEND_TIMEOUT):
        return await bot_for(method)(method, request_timeout=SEND_TIMEOUT)


@dataclass
class Round:
    token: str
    puzzle: Puzzle | None = None
    options: list[MoveOption] = field(default_factory=list)
    message: Message | None = None
    votes: dict[int, tuple[int, str]] = field(default_factory=dict)
    usernames: dict[int, str | None] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None
    timer: asyncio.TimerHandle | None = None
    closed: bool = False
    deadline: float = 0.0
    score_day: date | None = None
    revealed: bool = False
    scored: bool | None = None
    page: int = 0
    view: View | None = None
    markup: InlineKeyboardMarkup | None = None
    last_edit: float = 0.0
    solution_photo: bytes | None = None
    solution_shown: bool = False
    board_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def today() -> date:
    return datetime.now(DAY_ZONE).date()


def score_key(chat_id: int, day: date | None = None) -> str:
    return f"msu_hub:chess:{chat_id}:{(day or today()).isoformat()}:scores"


# Store all players and the completion marker atomically. A retried round must not
# apply penalties or rewards twice when Redis's first response was lost.
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
    expires = int(datetime.combine(day + timedelta(days=2), time.min, DAY_ZONE).timestamp())
    args: list[str | int] = [expires, round_token]
    for user_id, name, username, delta in players:
        args.extend((str(user_id), name, username or "", delta))
    client = await redis.redis()
    await cast(Awaitable[int], client.eval(_SAVE_SCORES, 4, key, key + ":names", key + ":usernames", key + ":rounds", *args))


class ChessCallback(CallbackData, prefix="chess"):
    round: str
    choice: str


class Chess:
    callback_data = ChessCallback
    rounds: dict[int, Round] = {}
    recent_puzzles: LRUCache[int, tuple[str, ...]] = LRUCache(maxsize=1024)
    completed: TTLCache[tuple[int, str], Round] = TTLCache(maxsize=MAX_ROUNDS, ttl=RESULT_TTL)

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
            try:
                async with round_.state_lock:
                    await cls.persist(round_, redis)
            except Exception:
                logger.exception("Chess initial state could not be saved")
            finally:
                cls.arm_timer(chat_id, round_, redis, supervisor)
        except ExternalServiceError, TelegramAPIError, asyncio.TimeoutError, ValueError, OSError:
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
        recent = cls.recent_puzzles.get(chat_id, ())
        puzzle = await random_puzzle(recent)
        image = await asyncio.to_thread(render_board, puzzle.fen)
        round_.puzzle, round_.options = puzzle, list(puzzle.options)
        view = cls.render_view(round_)
        markup = cls.keyboard(round_, view)
        round_.message = await _send(
            message.reply_photo(
                BufferedInputFile(image, filename="chess.png"),
                caption=view.caption,
                caption_entities=view.entities,
                parse_mode=None,
                reply_markup=markup,
            )
        )
        round_.deadline = wall_time() + ROUND_TIMEOUT
        round_.view, round_.markup = view, markup
        round_.last_edit = asyncio.get_running_loop().time()
        # A failed download, render or Telegram upload must not consume history.
        cls.recent_puzzles[chat_id] = (*recent, puzzle.id)[-15:]

    @staticmethod
    def render_view(round_: Round) -> View:
        assert round_.puzzle is not None
        players = [
            Player(
                uid, name, round_.usernames.get(uid), round_.options[choice].label, round_.options[choice].uci == round_.puzzle.solution[0]
            )
            for uid, (choice, name) in round_.votes.items()
        ]
        return render(round_.puzzle, players, closed=round_.closed, scored=round_.scored, page=round_.page)

    @staticmethod
    def keyboard(round_: Round, view: View) -> InlineKeyboardMarkup | None:
        keyboard = InlineKeyboardBuilder()
        if not round_.closed:
            keyboard.add(
                *[
                    InlineKeyboardButton(text=option.label, callback_data=ChessCallback(round=round_.token, choice=str(index)).pack())
                    for index, option in enumerate(round_.options)
                ]
            )
            keyboard.adjust(2)
            keyboard.row(
                InlineKeyboardButton(text="Завершить задание", callback_data=ChessCallback(round=round_.token, choice="finish").pack())
            )
        if view.pages > 1:
            buttons = [
                InlineKeyboardButton(text=label, callback_data=ChessCallback(round=round_.token, choice=f"page_{page}").pack())
                for label, page in (("‹", view.page - 1), (f"{view.page + 1}/{view.pages}", view.page), ("›", view.page + 1))
                if 0 <= page < view.pages
            ]
            keyboard.row(*buttons)
        return keyboard.as_markup() if keyboard.export() else None

    @staticmethod
    async def persist(round_: Round, redis: RedisStorage) -> None:
        assert round_.puzzle is not None and round_.message is not None
        await quiz_store.save(
            "chess",
            quiz_store.SavedRound(
                token=round_.token,
                message=round_.message,
                question=PUZZLE_ADAPTER.dump_json(round_.puzzle).decode(),
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
    def arm_timer(cls, chat_id: int, round_: Round, redis: RedisStorage, supervisor: Supervisor) -> None:
        if round_.timer is not None:
            round_.timer.cancel()
        if round_.closed:
            delay: float = RETRY_INTERVAL
            if round_.scored is True and round_.revealed:
                return
            if wall_time() + delay >= round_.deadline + RESULT_TTL:
                return
        else:
            delay = max(0, round_.deadline - wall_time())
        round_.timer = asyncio.get_running_loop().call_later(delay, cls.start_finish, chat_id, round_, redis, supervisor)

    @classmethod
    def recover(cls, saved: quiz_store.SavedRound, redis: RedisStorage, supervisor: Supervisor) -> Round:
        chat_id = saved.message.chat.id
        current = cls.rounds.get(chat_id)
        if current is not None and current.token == saved.token:
            return current
        cached = cls.completed.get((chat_id, saved.token))
        if cached is not None:
            return cached
        puzzle = PUZZLE_ADAPTER.validate_json(saved.question)
        round_ = Round(
            token=saved.token,
            puzzle=puzzle,
            options=list(puzzle.options),
            message=saved.message,
            votes=saved.votes,
            usernames=saved.usernames,
            deadline=saved.deadline,
            closed=saved.closed,
            score_day=saved.score_day,
            scored=saved.scored,
            revealed=saved.revealed,
        )
        if any(not 0 <= choice < len(round_.options) for choice, _ in round_.votes.values()):
            raise ValueError("Invalid saved chess answer")
        if round_.closed or current is not None:
            cls.completed[chat_id, round_.token] = round_
        else:
            cls.rounds[chat_id] = round_
        if round_.closed or round_.deadline <= wall_time() or current is not None:
            cls.start_finish(chat_id, round_, redis, supervisor)
        else:
            cls.arm_timer(chat_id, round_, redis, supervisor)
        return round_

    @classmethod
    async def restore(cls, bot: Bot, redis: RedisStorage, supervisor: Supervisor) -> None:
        for saved in sorted(await quiz_store.pending("chess", bot, redis), key=lambda item: item.deadline, reverse=True):
            try:
                cls.recover(saved, redis, supervisor)
            except ValueError:
                logger.exception("Saved chess round is invalid")

    @classmethod
    def start_finish(cls, chat_id: int, round_: Round, redis: RedisStorage, supervisor: Supervisor) -> asyncio.Task[None] | None:
        # Claim completion without yielding: a timer and a click may arrive together.
        owned = cls.rounds.get(chat_id) is round_ or cls.completed.get((chat_id, round_.token)) is round_
        if not owned or (round_.task is not None and not round_.task.done()):
            return None
        if round_.closed and round_.scored is True and round_.revealed:
            return None
        if not round_.closed:
            round_.closed = True
        if round_.score_day is None:
            round_.score_day = datetime.fromtimestamp(round_.deadline, DAY_ZONE).date() if wall_time() >= round_.deadline else today()
        if round_.timer is not None:
            round_.timer.cancel()
        try:
            round_.task = supervisor.create_job(lambda: cls.finish(chat_id, round_, redis, supervisor))
        except AdmissionClosed:
            # The saved snapshot is recovered by the next application instance.
            return None
        return round_.task

    @classmethod
    async def process_cb(
        cls, query: CallbackQuery, callback_data: ChessCallback, redis: RedisStorage, supervisor: Supervisor
    ) -> bool | None:
        if not isinstance(query.message, Message):
            return await _send(query.answer("Этот раунд недоступен."))
        message = query.message
        round_ = cls.rounds.get(message.chat.id)
        if round_ is None or round_.token != callback_data.round:
            round_ = cls.completed.get((message.chat.id, callback_data.round))
        if round_ is None:
            try:
                saved = await quiz_store.load("chess", bot_for(message), message.chat.id, callback_data.round, redis)
                if saved is not None and saved.message.message_id == message.message_id:
                    round_ = cls.recover(saved, redis, supervisor)
            except Exception:
                logger.exception("Chess round could not be restored")
        if round_ is None or round_.message is None or round_.message.message_id != message.message_id:
            return await _send(query.answer("Раунд недоступен. Начни новый: /chess", show_alert=True))
        if not round_.closed and wall_time() >= round_.deadline:
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
                await _send(query.answer("Раунд завершён. Начни новый: /chess", show_alert=True))
            finally:
                # A retry uses the frozen score day and the round's idempotency key.
                task = cls.start_finish(message.chat.id, round_, redis, supervisor)
                if task is not None:
                    await asyncio.shield(task)
                elif round_.task is None or round_.task.done():
                    await cls.update_board(round_, page=0)
            return None
        if callback_data.choice == "finish":
            task = cls.start_finish(message.chat.id, round_, redis, supervisor)
            await _send(query.answer("Задание завершено!"))
            if task is not None:
                await asyncio.shield(task)
            return None
        user_id = query.from_user.id
        try:
            choice = int(callback_data.choice)
            if not 0 <= choice < len(round_.options):
                raise ValueError
        except ValueError:
            return await _send(query.answer("Неизвестный вариант."))
        feedback: str | None = None
        async with round_.state_lock:
            if round_.closed or wall_time() >= round_.deadline:
                cls.start_finish(message.chat.id, round_, redis, supervisor)
                feedback = "Раунд завершён. Начни новый: /chess"
            else:
                duplicate = user_id in round_.votes
                if not duplicate:
                    round_.votes[user_id] = (choice, query.from_user.full_name)
                    round_.usernames[user_id] = query.from_user.username
                try:
                    # Keep the immutable choice if Redis committed but its reply was lost.
                    # Repeating a click confirms the same snapshot before acknowledging it.
                    await cls.persist(round_, redis)
                except Exception:
                    logger.exception("Chess vote persistence was not confirmed")
                    feedback = "Не удалось подтвердить ответ. Нажми тот же вариант ещё раз."
                else:
                    if duplicate:
                        feedback = "Твой ответ уже принят. Изменить его нельзя."
        if feedback is not None:
            return await _send(query.answer(feedback, show_alert=True))
        try:
            await _send(query.answer("Ответ принят! Результат — в конце раунда."))
        finally:
            await cls.update_board(round_)
        return None

    @staticmethod
    async def edit(round_: Round, method: TelegramMethod[Message | bool]) -> bool:
        # Failed edits also count towards the pacing budget, including fallbacks.
        await asyncio.sleep(max(0, round_.last_edit + EDIT_INTERVAL - asyncio.get_running_loop().time()))
        try:
            await _send(method)
        except TelegramBadRequest as error:
            if not error.message.removeprefix("Bad Request: ").casefold().startswith("message is not modified"):
                logger.warning("Chess message update failed")
                return False
        except TelegramAPIError, asyncio.TimeoutError:
            logger.warning("Chess message update failed")
            return False
        finally:
            round_.last_edit = asyncio.get_running_loop().time()
        return True

    @classmethod
    async def update_board(cls, round_: Round, *, page: int | None = None) -> None:
        try:
            async with asyncio.timeout(UPDATE_TIMEOUT):
                await cls._update_board(round_, page=page)
        except TimeoutError:
            logger.warning("Chess result delivery timed out")

    @classmethod
    async def _update_board(cls, round_: Round, *, page: int | None = None) -> None:
        if round_.message is None or round_.puzzle is None:
            return
        async with round_.board_lock:
            # Persist scores before publishing the answer or waiting for delivery.
            if round_.closed and round_.scored is None:
                return
            if page is not None:
                round_.page = page
            view = cls.render_view(round_)
            markup = cls.keyboard(round_, view)
            needs_photo = round_.closed and not round_.solution_shown
            if view == round_.view and markup == round_.markup and not needs_photo:
                if round_.closed:
                    round_.revealed = True
                return
            # Read state again after waiting so a burst of votes becomes one edit.
            await asyncio.sleep(max(0, round_.last_edit + EDIT_INTERVAL - asyncio.get_running_loop().time()))
            if round_.closed and round_.scored is None:
                return
            view = cls.render_view(round_)
            markup = cls.keyboard(round_, view)
            delivered = False
            closed_view, scored_view = round_.closed, round_.scored
            if round_.closed and not round_.solution_shown:
                if round_.solution_photo is None:
                    try:
                        round_.solution_photo = await asyncio.wait_for(
                            asyncio.to_thread(render_board, round_.puzzle.fen, arrow=round_.puzzle.solution[0]), timeout=5
                        )
                    except asyncio.TimeoutError, ValueError, OSError:
                        logger.warning("Chess solution rendering failed")
                if round_.solution_photo is not None:
                    delivered = await cls.edit(
                        round_,
                        round_.message.edit_media(
                            InputMediaPhoto(
                                media=BufferedInputFile(round_.solution_photo, filename="chess-solution.png"),
                                caption=view.caption,
                                caption_entities=view.entities,
                                parse_mode=None,
                            ),
                            reply_markup=markup,
                        ),
                    )
                    round_.solution_shown = delivered
            if not delivered:
                delivered = await cls.edit(
                    round_,
                    round_.message.edit_caption(caption=view.caption, caption_entities=view.entities, parse_mode=None, reply_markup=markup),
                )
            if delivered:
                round_.page, round_.view, round_.markup = view.page, view, markup
                if closed_view and round_.closed and scored_view == round_.scored and view == cls.render_view(round_):
                    round_.revealed = True

    @classmethod
    async def finish(cls, chat_id: int, round_: Round, redis: RedisStorage, supervisor: Supervisor) -> None:
        cancelled = False
        persisted = False
        try:
            puzzle = round_.puzzle
            if puzzle is None or round_.message is None:
                return
            async with round_.state_lock:
                # Freeze closure/day durably before the idempotent scoring operation.
                if round_.score_day is None:
                    round_.score_day = today()
                try:
                    await cls.persist(round_, redis)
                except Exception:
                    logger.exception("Chess closure could not be saved")
                    if round_.scored is not True:
                        round_.scored = False
                else:
                    if round_.scored is not True:
                        try:
                            players = [
                                (uid, name, round_.usernames.get(uid), 1 if round_.options[choice].uci == puzzle.solution[0] else -1)
                                for uid, (choice, name) in round_.votes.items()
                            ]
                            await asyncio.wait_for(
                                save_scores(chat_id, players, redis, round_.score_day, round_token=round_.token), timeout=5
                            )
                            round_.scored = True
                        except Exception:
                            round_.scored = False
                            logger.exception("Chess score update failed")
                        round_.revealed = False
            # Delivery cannot keep the next game blocked, even behind an edit lock.
            cls.completed[chat_id, round_.token] = round_
            if cls.rounds.get(chat_id) is round_:
                cls.rounds.pop(chat_id, None)
            await cls.update_board(round_, page=0)
            async with round_.state_lock:
                await cls.persist(round_, redis)
                persisted = True
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            logger.exception("Chess round could not be completed")
        finally:
            if cls.rounds.get(chat_id) is round_ and round_.closed:
                cls.completed[chat_id, round_.token] = round_
                cls.rounds.pop(chat_id, None)
            if not cancelled and (not persisted or round_.scored is not True or not round_.revealed):
                # If the successful reveal was not saved, preserve a retry obligation.
                if not persisted:
                    round_.revealed = False
                cls.arm_timer(chat_id, round_, redis, supervisor)

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
        body = Text(*[Text(row, "\n") for row in rows]) if rows else Text("Пока нет очков. Начни /chess")
        text, entities = Text(f"🏆 Шахматный рейтинг за сегодня, {day:%d.%m.%Y} (МСК)\n\n", body).render()
        return await _send(message.reply(text, entities=entities, parse_mode=None))

    @classmethod
    async def shutdown(cls) -> None:
        rounds = {id(round_): round_ for round_ in (*cls.rounds.values(), *cls.completed.values())}.values()
        tasks = []
        for round_ in rounds:
            if round_.timer is not None:
                round_.timer.cancel()
            if round_.task is not None and not round_.task.done():
                tasks.append(round_.task)
                round_.task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        cls.rounds.clear()
        cls.recent_puzzles.clear()
        cls.completed.clear()
