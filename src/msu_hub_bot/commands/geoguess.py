import asyncio
import logging
import random
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from html import escape
from typing import Optional, TypeVar, cast
from collections.abc import Awaitable

from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.methods import TelegramMethod
from aiogram.exceptions import TelegramAPIError
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.runtime import AdmissionClosed, Supervisor
from msu_hub_bot.telegram.storage import RedisStorage

from msu_hub_bot.providers.exceptions import ExternalServiceError
from msu_hub_bot.providers.geoguess import COUNTRIES, Photo, random_photo

logger = logging.getLogger(__name__)
SEND_TIMEOUT = 15
PHOTO_TIMEOUT = 10
ROUND_TIMEOUT = 10 * 60
DAY_ZONE = ZoneInfo("Europe/Moscow")
COUNTRY_CODES = {name: code for code, name in COUNTRIES.items()}
MAX_ROUNDS = 128
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
    board: Optional[Message] = None
    board_more: list[Message] = field(default_factory=list)
    board_texts: list[str] = field(default_factory=list)
    board_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def credit(photo: Photo) -> str:
    return f'Фото: {escape(photo.author)}, <a href="{escape(photo.license_url, quote=True)}">{escape(photo.license)}</a>.'


def today() -> date:
    return datetime.now(DAY_ZONE).date()


def score_key(chat_id: int, day: date | None = None) -> str:
    return f"msu_hub:geoguess:{chat_id}:{(day or today()).isoformat()}:scores"


def country_label(country: str) -> str:
    code = COUNTRY_CODES.get(country)
    if code is None:
        return country
    flag = "".join(chr(0x1F1E6 + ord(letter) - ord("a")) for letter in code)
    return f"{flag} {country}"


def user_label(user_id: int, name: str, username: str | None) -> str:
    name = escape(name)
    return f"{name} (@{escape(username)})" if username else f'<a href="tg://user?id={user_id}">{name}</a>'


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
        photo = await random_photo()
        options = random.sample(sorted(set(COUNTRIES.values()) - {photo.country}), 5) + [photo.country]
        random.shuffle(options)
        keyboard = InlineKeyboardBuilder()
        keyboard.add(
            *[
                InlineKeyboardButton(text=country_label(country), callback_data=GeoguessCallback(round=round_.token, choice=str(i)).pack())
                for i, country in enumerate(options)
            ]
        )
        keyboard.adjust(2)
        keyboard.row(
            InlineKeyboardButton(text="Завершить задание", callback_data=GeoguessCallback(round=round_.token, choice="finish").pack())
        )
        round_.photo, round_.options = photo, options
        round_.message = await _send(
            message.reply_photo(
                photo.url,
                reply_markup=keyboard.as_markup(),
            )
        )

    @classmethod
    def start_finish(cls, chat_id: int, round_: Round, redis: RedisStorage, supervisor: Supervisor) -> asyncio.Task[None] | None:
        # Both the timer and buttons claim completion before yielding control.
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
        cls, query: CallbackQuery, callback_data: GeoguessCallback, redis: RedisStorage, supervisor: Supervisor
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
            or round_.message.message_id != query.message.message_id
        ):
            return await _send(query.answer("Раунд завершён. Начни новый: /geoguess", show_alert=True))
        if callback_data.choice == "finish":
            task = cls.start_finish(message.chat.id, round_, redis, supervisor)
            await _send(query.answer("Задание завершено!"))
            # The supervisor owns completion even if the callback worker stops.
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
        except KeyError, ValueError:
            return await _send(query.answer("Неизвестный вариант."))
        # No await between checking and recording: simultaneous clicks cannot vote twice.
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
                for index, country in enumerate(round_.options):
                    voters = [(uid, name) for uid, (choice, name) in round_.votes.items() if choice == index]
                    lines.append(f"\n{escape(country_label(country))} ({len(voters)}):")
                    lines.extend(user_label(uid, name, round_.usernames.get(uid)) for uid, name in voters)
                    if not voters:
                        lines.append("никто")
            else:
                lines = [
                    f"🗳 Проголосовали: {len(round_.votes)}.",
                    "Выбор каждого покажу после завершения.",
                    "Завершить задание может любой. Автоматическое завершение — через 10 минут после появления фото.",
                ]
                lines.extend(user_label(uid, name, round_.usernames.get(uid)) for uid, (_, name) in round_.votes.items())
            chunks = [""]
            for line in lines:
                if len(chunks[-1]) + len(line) + 1 > 3000:
                    chunks.append("")
                chunks[-1] += line + "\n"
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
                # A shorter final heading can occasionally reduce the number of pages.
                boards = ([round_.board] if round_.board is not None else []) + round_.board_more
                for i in range(len(chunks), len(boards)):
                    if round_.board_texts[i] != "Список ответов выше.":
                        await _send(boards[i].edit_text("Список ответов выше."))
                        round_.board_texts[i] = "Список ответов выше."
            except TelegramAPIError, asyncio.TimeoutError:
                logger.warning("Geoguess vote board update failed")

    @classmethod
    async def finish(cls, chat_id: int, round_: Round, redis: RedisStorage) -> None:
        try:
            round_.closed = True
            day = today()  # A round belongs to the day it finishes, even across midnight.
            await cls.update_board(round_)
            photo = round_.photo
            if photo is None or round_.message is None:
                return
            winners = [(uid, name) for uid, (choice, name) in round_.votes.items() if round_.options[choice] == photo.country]
            scored = True
            try:
                players = [
                    (uid, name, round_.usernames.get(uid), 1 if round_.options[choice] == photo.country else -1)
                    for uid, (choice, name) in round_.votes.items()
                ]
                await asyncio.wait_for(save_scores(chat_id, players, redis, day, round_token=round_.token), timeout=5)
            except Exception:
                scored = False
                logger.exception("Geoguess score update failed")
            place = ", ".join(part for part in (photo.city, country_label(photo.country)) if part)
            result = (
                f"🌍 На снимке — <b>{escape(place)}</b>.\n\n"
                f'{credit(photo)}\n<a href="{photo.source}">Источник фотографии</a>\n'
                'Геоданные: <a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>\n\n'
            )
            if winners:
                mentions = [user_label(uid, name, round_.usernames.get(uid)) for uid, name in winners]
                heading = f"Угадали {len(winners)} из {len(round_.votes)}"
                points = "Каждому +1 очко. За ошибку −1 очко, минимум за день — 0." if scored else "Не удалось подтвердить запись очков."
                listing = ", ".join(mentions)
                # Long winner lists are sent separately; never omit participants.
                if len(result + heading + listing + points) < 950:
                    result += f"{heading}: {listing}.\n{points}"
                    winner_messages = []
                else:
                    result += f"{heading}.\n{points}\nПобедители — в сообщении ниже."
                    winner_messages = ["🏆 Победители:\n"]
                    for mention in mentions:
                        if len(winner_messages[-1]) + len(mention) + 1 > 3000:
                            winner_messages.append("🏆 Победители (продолжение):\n")
                        winner_messages[-1] += mention + "\n"
            else:
                winner_messages = []
                result += "Никто не угадал 😄" if round_.votes else "В этот раз никто не ответил."
                if round_.votes:
                    result += "\nЗа ошибку −1 очко, минимум за день — 0." if scored else "\nНе удалось подтвердить запись очков."
            try:
                await _send(round_.message.edit_caption(caption=result, parse_mode="HTML", reply_markup=None))
            except TelegramAPIError, asyncio.TimeoutError:
                await _send(round_.message.reply(result, parse_mode="HTML", disable_web_page_preview=True))
            for text in winner_messages:
                await _send(round_.message.reply(text, parse_mode="HTML", disable_web_page_preview=True))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Geoguess round could not be completed")
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
        text = f"🏆 Рейтинг за сегодня, {day:%d.%m.%Y} (МСК)\n\n" + ("\n".join(rows) if rows else "Пока нет очков. Начни /geoguess")
        return await _send(message.reply(text, parse_mode="HTML"))

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
