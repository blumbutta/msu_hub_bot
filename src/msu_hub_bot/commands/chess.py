"""Chat chess puzzles with hidden votes and a daily, chat-local leaderboard."""

import asyncio
import logging
import secrets
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from html import escape
from typing import TypeVar, cast
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.methods import TelegramMethod
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InputMediaPhoto, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from cachetools import LRUCache

from msu_hub_bot.media.chessboard import render_board
from msu_hub_bot.providers.chess import MoveOption, Puzzle, random_puzzle
from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.runtime import AdmissionClosed, Supervisor
from msu_hub_bot.telegram.storage import RedisStorage

logger = logging.getLogger(__name__)
SEND_TIMEOUT = 15
PHOTO_TIMEOUT = 10
ROUND_TIMEOUT = 10 * 60
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
    board: Message | None = None
    board_more: list[Message] = field(default_factory=list)
    board_texts: list[str] = field(default_factory=list)
    board_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def today() -> date:
    return datetime.now(DAY_ZONE).date()


def score_key(chat_id: int, day: date | None = None) -> str:
    return f"msu_hub:chess:{chat_id}:{(day or today()).isoformat()}:scores"


def user_label(user_id: int, name: str, username: str | None) -> str:
    # Telegram names and usernames are bounded; keep old Redis entries bounded too.
    name = escape(name[:129])
    return f"{name} (@{escape(username[:32])})" if username else f'<a href="tg://user?id={user_id}">{name}</a>'


def message_pages(lines: list[str]) -> list[str]:
    """Split on complete HTML lines so long participant lists remain readable."""
    pages = [""]
    for line in lines:
        if pages[-1] and len(pages[-1]) + len(line) + 1 > 3000:
            pages.append("")
        pages[-1] += line + "\n"
    return pages


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
            round_.timer = asyncio.get_running_loop().call_later(ROUND_TIMEOUT, cls.start_finish, chat_id, round_, redis, supervisor)
            await cls.update_board(round_)
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
        keyboard = InlineKeyboardBuilder()
        keyboard.add(
            *[
                InlineKeyboardButton(text=option.label, callback_data=ChessCallback(round=round_.token, choice=str(index)).pack())
                for index, option in enumerate(puzzle.options)
            ]
        )
        keyboard.adjust(2)
        keyboard.row(
            InlineKeyboardButton(text="Завершить задание", callback_data=ChessCallback(round=round_.token, choice="finish").pack())
        )
        side = "белых" if puzzle.fen.split()[1] == "w" else "чёрных"
        round_.puzzle, round_.options = puzzle, list(puzzle.options)
        round_.message = await _send(
            message.reply_photo(
                BufferedInputFile(image, filename="chess.png"),
                caption=f"♟ Ход {side}. Найдите лучший ход.",
                reply_markup=keyboard.as_markup(),
            )
        )
        # A failed download, render or Telegram upload must not consume history.
        cls.recent_puzzles[chat_id] = (*recent, puzzle.id)[-15:]

    @classmethod
    def start_finish(cls, chat_id: int, round_: Round, redis: RedisStorage, supervisor: Supervisor) -> asyncio.Task[None] | None:
        # Claim completion without yielding: a timer and a click may arrive together.
        if cls.rounds.get(chat_id) is not round_ or round_.closed:
            return None
        round_.closed = True
        if round_.timer is not None:
            round_.timer.cancel()
        try:
            round_.task = supervisor.create_job(lambda: cls.finish(chat_id, round_, redis))
        except AdmissionClosed:
            cls.rounds.pop(chat_id, None)
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
        if (
            round_ is None
            or round_.message is None
            or round_.closed
            or round_.token != callback_data.round
            or round_.message.message_id != message.message_id
        ):
            return await _send(query.answer("Раунд завершён. Начни новый: /chess", show_alert=True))
        if callback_data.choice == "finish":
            task = cls.start_finish(message.chat.id, round_, redis, supervisor)
            await _send(query.answer("Задание завершено!"))
            if task is not None:
                await asyncio.shield(task)
            return None
        user_id = query.from_user.id
        if user_id in round_.votes:
            return await _send(query.answer("Твой ответ уже принят. Изменить его нельзя.", show_alert=True))
        try:
            choice = int(callback_data.choice)
            if not 0 <= choice < len(round_.options):
                raise ValueError
        except ValueError:
            return await _send(query.answer("Неизвестный вариант."))
        round_.votes[user_id] = (choice, query.from_user.full_name)
        round_.usernames[user_id] = query.from_user.username
        try:
            await _send(query.answer("Ответ принят! Результат — в конце раунда."))
        finally:
            await cls.update_board(round_)
        return None

    @classmethod
    async def update_board(cls, round_: Round) -> None:
        if round_.message is None:
            return
        async with round_.board_lock:
            if round_.closed:
                lines = [f"🏁 Голосование завершено. Проголосовали: {len(round_.votes)}."]
                correct = round_.puzzle.solution[0] if round_.puzzle is not None else None
                for index, option in enumerate(round_.options):
                    voters = [(uid, name) for uid, (choice, name) in round_.votes.items() if choice == index]
                    mark = "✅" if option.uci == correct else "❌"
                    lines.append(f"\n{mark} {escape(option.label)} ({len(voters)}):")
                    lines.extend(user_label(uid, name, round_.usernames.get(uid)) for uid, name in voters)
                    if not voters:
                        lines.append("никто")
            else:
                lines = [
                    f"🗳 Проголосовали: {len(round_.votes)}.",
                    "Выбор каждого покажу после завершения.",
                    "Завершить задание может любой. Автоматическое завершение — через 10 минут после появления доски.",
                ]
                lines.extend(user_label(uid, name, round_.usernames.get(uid)) for uid, (_, name) in round_.votes.items())
            chunks = message_pages(lines)
            try:
                for i, text in enumerate(chunks):
                    boards = ([round_.board] if round_.board is not None else []) + round_.board_more
                    if i >= len(boards):
                        board = await _send(round_.message.reply(text, parse_mode="HTML"))
                        if i == 0:
                            round_.board = board
                        else:
                            round_.board_more.append(board)
                        round_.board_texts.append(text)
                    elif round_.board_texts[i] != text:
                        await _send(boards[i].edit_text(text, parse_mode="HTML"))
                        round_.board_texts[i] = text
                boards = ([round_.board] if round_.board is not None else []) + round_.board_more
                for i in range(len(chunks), len(boards)):
                    if round_.board_texts[i] != "Список ответов выше.":
                        await _send(boards[i].edit_text("Список ответов выше."))
                        round_.board_texts[i] = "Список ответов выше."
            except TelegramAPIError, asyncio.TimeoutError:
                logger.warning("Chess vote board update failed")

    @classmethod
    async def reveal(cls, round_: Round, lines: list[str]) -> None:
        """Reveal the arrow when possible and always attempt to deliver the solution."""
        puzzle, message = round_.puzzle, round_.message
        if puzzle is None or message is None:
            return
        pages = message_pages(lines)
        short_result = len(pages) == 1 and len(pages[0]) <= 950
        caption = pages[0] if short_result else "♟ Задание завершено. Решение и результаты — ниже."
        delivered = False
        try:
            image = await asyncio.wait_for(asyncio.to_thread(render_board, puzzle.fen, arrow=puzzle.solution[0]), timeout=5)
            await _send(
                message.edit_media(
                    InputMediaPhoto(media=BufferedInputFile(image, filename="chess-solution.png"), caption=caption, parse_mode="HTML"),
                    reply_markup=None,
                )
            )
            delivered = True
        except TelegramAPIError, asyncio.TimeoutError, ValueError, OSError:
            logger.warning("Chess solution board update failed")
            try:
                await _send(message.edit_caption(caption=caption, parse_mode="HTML", reply_markup=None))
                delivered = True
            except TelegramAPIError, asyncio.TimeoutError:
                logger.warning("Chess solution caption update failed")
        if not short_result or not delivered:
            for text in pages:
                await _send(message.reply(text, parse_mode="HTML", disable_web_page_preview=True))

    @classmethod
    async def finish(cls, chat_id: int, round_: Round, redis: RedisStorage) -> None:
        try:
            round_.closed = True
            day = today()
            await cls.update_board(round_)
            puzzle = round_.puzzle
            if puzzle is None or round_.message is None:
                return
            correct = puzzle.solution[0]
            winners = [(uid, name) for uid, (choice, name) in round_.votes.items() if round_.options[choice].uci == correct]
            scored = True
            try:
                players = [
                    (uid, name, round_.usernames.get(uid), 1 if round_.options[choice].uci == correct else -1)
                    for uid, (choice, name) in round_.votes.items()
                ]
                await asyncio.wait_for(save_scores(chat_id, players, redis, day, round_token=round_.token), timeout=5)
            except Exception:
                scored = False
                logger.exception("Chess score update failed")
            label = next(option.label for option in round_.options if option.uci == correct)
            lines = [f"♟ Правильный ход: <b>{escape(label)}</b>.", "\nПродолжение:"]
            # Keep a long line of play split into complete, readable moves.
            move_number = int(puzzle.fen.split()[5])
            white = puzzle.fen.split()[1] == "w"
            for san in puzzle.line:
                lines.append(f"{move_number}{'.' if white else '...'} <code>{escape(san)}</code>")
                if not white:
                    move_number += 1
                white = not white
            lines.append(f'\n<a href="https://lichess.org/training/{puzzle.id}">Задача на Lichess</a>')
            if winners:
                lines.append(f"\nУгадали {len(winners)} из {len(round_.votes)}:")
                lines.extend(user_label(uid, name, round_.usernames.get(uid)) for uid, name in winners)
            else:
                lines.append("\nНикто не угадал 😄" if round_.votes else "\nВ этот раз никто не ответил.")
            if round_.votes:
                lines.append(
                    "Правильный ответ: +1 очко. За ошибку −1 очко, минимум за день — 0."
                    if scored
                    else "Не удалось подтвердить запись очков."
                )
            await cls.reveal(round_, lines)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Chess round could not be completed")
        finally:
            if round_.timer is not None:
                round_.timer.cancel()
            if cls.rounds.get(chat_id) is round_:
                cls.rounds.pop(chat_id, None)

    @classmethod
    async def top(cls, message: Message, redis: RedisStorage) -> Message:
        day = today()
        key = score_key(message.chat.id, day)

        async def read() -> list[str]:
            client = await redis.redis()
            scores = await cast(Awaitable[list[tuple[str, float]]], client.zrevrange(key, 0, 9, withscores=True))
            rows: list[str] = []
            for user_id, score in scores:
                name = await cast(Awaitable[str | None], client.hget(key + ":names", user_id))
                if isinstance(name, bytes):
                    name = name.decode("utf-8", errors="replace")
                username = await cast(Awaitable[str | None], client.hget(key + ":usernames", user_id))
                if isinstance(username, bytes):
                    username = username.decode("utf-8", errors="replace")
                label = user_label(int(user_id), name or "Игрок", username)
                rows.append(f"{len(rows) + 1}. {label} — {int(score)}")
            return rows

        try:
            rows = await asyncio.wait_for(read(), timeout=5)
        except Exception:
            return await _send(message.reply("Рейтинг сейчас недоступен."))
        lines = [f"🏆 Шахматный рейтинг за сегодня, {day:%d.%m.%Y} (МСК)\n", *(rows or ["Пока нет очков. Начни /chess"])]
        result = message
        for text in message_pages(lines):
            result = await _send(message.reply(text, parse_mode="HTML"))
        return result

    @classmethod
    async def shutdown(cls) -> None:
        for round_ in cls.rounds.values():
            if round_.timer is not None:
                round_.timer.cancel()
        tasks = [round_.task for round_ in cls.rounds.values() if round_.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        cls.rounds.clear()
        cls.recent_puzzles.clear()
