import asyncio
import logging
import random
import secrets
from dataclasses import dataclass, field
from html import escape
from typing import Optional

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.exceptions import TelegramAPIError

from common.externals.exceptions import ExternalServiceError
from common.externals.geoguess import COUNTRIES, Photo, random_photo

logger = logging.getLogger(__name__)
SEND_TIMEOUT = 15
PHOTO_TIMEOUT = 10
MAX_ROUNDS = 128


@dataclass
class Round:
    token: str
    photo: Optional[Photo] = None
    options: list = field(default_factory=list)
    message: Optional[Message] = None
    votes: dict = field(default_factory=dict)
    usernames: dict = field(default_factory=dict)
    task: Optional[asyncio.Task] = None
    closed: bool = False
    board: Optional[Message] = None
    board_more: list = field(default_factory=list)
    board_texts: list = field(default_factory=list)
    board_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def credit(photo):
    return f'Фото: {escape(photo.author)}, <a href="{escape(photo.license_url, quote=True)}">{escape(photo.license)}</a>.'


def score_key(chat_id):
    return f'msu_hub:geoguess:{chat_id}:scores'


async def save_scores(chat_id, winners):
    if not winners:
        return
    from app import redis
    client = await redis.redis()
    async with client.pipeline(transaction=True) as pipe:
        for user_id, name in winners:
            pipe.zincrby(score_key(chat_id), 1, str(user_id))
            pipe.hset(score_key(chat_id) + ':names', str(user_id), name)
        await pipe.execute()


class Geoguess:
    callback_data = CallbackData('geoguess', 'round', 'choice')
    rounds = {}

    @classmethod
    async def process(cls, message: Message):
        chat_id = message.chat.id
        if chat_id in cls.rounds:
            return await message.reply('Подождите, прошлое задание еще не окончено!', request_timeout=SEND_TIMEOUT)
        if len(cls.rounds) >= MAX_ROUNDS:
            return await message.reply('Сейчас слишком много игр. Попробуй немного позже.', request_timeout=SEND_TIMEOUT)
        round_ = Round(secrets.token_hex(6))
        cls.rounds[chat_id] = round_
        started = False
        try:
            await asyncio.wait_for(cls.send_round_photo(message, round_), timeout=PHOTO_TIMEOUT)
            started = True
            await cls.update_board(round_)
        except (ExternalServiceError, TelegramAPIError, asyncio.TimeoutError):
            if cls.rounds.get(chat_id) is round_:
                cls.rounds.pop(chat_id, None)
            await message.reply('Ошибка, попробуйте еще раз', request_timeout=SEND_TIMEOUT)
        finally:
            if not started and cls.rounds.get(chat_id) is round_:
                cls.rounds.pop(chat_id, None)

    @classmethod
    async def send_round_photo(cls, message, round_):
        photo = await random_photo()
        options = random.sample(sorted(set(COUNTRIES.values()) - {photo.country}), 3) + [photo.country]
        random.shuffle(options)
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(*[
            InlineKeyboardButton(country, callback_data=cls.callback_data.new(round_.token, str(i)))
            for i, country in enumerate(options)
        ])
        keyboard.row(InlineKeyboardButton('Завершить задание', callback_data=cls.callback_data.new(round_.token, 'finish')))
        round_.photo, round_.options = photo, options
        round_.message = await message.reply_photo(
            photo.url,
            reply_markup=keyboard, request_timeout=SEND_TIMEOUT,
        )

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: dict):
        if query.message is None:
            return await query.answer('Этот раунд недоступен.')
        round_ = cls.rounds.get(query.message.chat.id)
        if (round_ is None or round_.message is None or round_.closed
                or round_.token != callback_data['round']
                or round_.message.message_id != query.message.message_id):
            return await query.answer('Раунд завершён. Начни новый: /geoguess', show_alert=True)
        if callback_data.get('choice') == 'finish':
            # Close before any await, so two clicks cannot award points twice.
            round_.closed = True
            round_.task = asyncio.create_task(cls.finish(query.message.chat.id, round_))
            await query.answer('Задание завершено!')
            return await round_.task
        user_id = query.from_user.id
        if user_id in round_.votes:
            return await query.answer('Твой ответ уже принят. Изменить его нельзя.', show_alert=True)
        try:
            choice = int(callback_data['choice'])
            if not 0 <= choice < len(round_.options):
                raise ValueError
        except (KeyError, ValueError):
            return await query.answer('Неизвестный вариант.')
        # No await between checking and recording: simultaneous clicks cannot vote twice.
        round_.votes[user_id] = (choice, query.from_user.full_name[:40])
        round_.usernames[user_id] = query.from_user.username
        try:
            await query.answer('Ответ принят! Результат — в конце раунда.')
        finally:
            await cls.update_board(round_)

    @classmethod
    async def update_board(cls, round_):
        async with round_.board_lock:
            lines = ['🏁 Голосование завершено.' if round_.closed else '🗳 Кто что выбрал:']
            for index, country in enumerate(round_.options):
                names = [name for choice, name in round_.votes.values() if choice == index]
                lines.append(f'\n{escape(country)} ({len(names)}):')
                lines.extend(escape(name) for name in names)
                if not names:
                    lines.append('пока никто')
            chunks = ['']
            for line in lines:
                if len(chunks[-1]) + len(line) + 1 > 3000:
                    chunks.append('')
                chunks[-1] += line + '\n'
            try:
                for i, text in enumerate(chunks):
                    boards = ([round_.board] if round_.board is not None else []) + round_.board_more
                    if i >= len(boards):
                        board = await round_.message.reply(text, parse_mode='HTML', request_timeout=SEND_TIMEOUT)
                        if i == 0:
                            round_.board = board
                        else:
                            round_.board_more.append(board)
                        round_.board_texts.append(text)
                    elif round_.board_texts[i] != text:
                        await boards[i].edit_text(text, parse_mode='HTML', request_timeout=SEND_TIMEOUT)
                        round_.board_texts[i] = text
                # A shorter final heading can occasionally reduce the number of pages.
                boards = ([round_.board] if round_.board is not None else []) + round_.board_more
                for i in range(len(chunks), len(boards)):
                    if round_.board_texts[i] != 'Список ответов выше.':
                        await boards[i].edit_text('Список ответов выше.', request_timeout=SEND_TIMEOUT)
                        round_.board_texts[i] = 'Список ответов выше.'
            except (TelegramAPIError, asyncio.TimeoutError):
                logger.warning('Geoguess vote board update failed')

    @classmethod
    async def finish(cls, chat_id, round_):
        try:
            round_.closed = True
            await cls.update_board(round_)
            photo = round_.photo
            winners = [(uid, name) for uid, (choice, name) in round_.votes.items() if round_.options[choice] == photo.country]
            scored = True
            try:
                await asyncio.wait_for(save_scores(chat_id, winners), timeout=5)
            except Exception:
                scored = False
                logger.exception('Geoguess score update failed')
            place = ', '.join(part for part in (photo.city, photo.country) if part)
            result = (
                f'🌍 На снимке — <b>{escape(place)}</b>.\n\n'
                f'{credit(photo)}\n<a href="{photo.source}">Источник фотографии</a>\n'
                'Геоданные: <a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>\n\n'
            )
            if winners:
                mentions = [
                    '@' + escape(round_.usernames[uid]) if round_.usernames.get(uid)
                    else f'<a href="tg://user?id={uid}">{escape(name)}</a>'
                    for uid, name in winners
                ]
                heading = f'Угадали {len(winners)} из {len(round_.votes)}'
                points = 'Каждому +1 очко.' if scored else 'Не удалось подтвердить запись очков.'
                listing = ', '.join(mentions)
                # Long winner lists are sent separately; never omit participants.
                if len(result + heading + listing + points) < 950:
                    result += f'{heading}: {listing}.\n{points}'
                    winner_messages = []
                else:
                    result += f'{heading}.\n{points}\nПобедители — в сообщении ниже.'
                    winner_messages = ['🏆 Победители:\n']
                    for mention in mentions:
                        if len(winner_messages[-1]) + len(mention) + 1 > 3000:
                            winner_messages.append('🏆 Победители (продолжение):\n')
                        winner_messages[-1] += mention + '\n'
            else:
                winner_messages = []
                result += 'Никто не угадал 😄' if round_.votes else 'В этот раз никто не ответил.'
            try:
                await round_.message.edit_caption(result, parse_mode='HTML', reply_markup=None, request_timeout=SEND_TIMEOUT)
            except (TelegramAPIError, asyncio.TimeoutError):
                await round_.message.reply(result, parse_mode='HTML', disable_web_page_preview=True, request_timeout=SEND_TIMEOUT)
            for text in winner_messages:
                await round_.message.reply(text, parse_mode='HTML', disable_web_page_preview=True, request_timeout=SEND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Geoguess round could not be completed')
        finally:
            if cls.rounds.get(chat_id) is round_:
                cls.rounds.pop(chat_id, None)

    @classmethod
    async def top(cls, message: Message):
        from app import redis

        async def read():
            client = await redis.redis()
            scores = await client.zrevrange(score_key(message.chat.id), 0, 9, withscores=True)
            rows = []
            for user_id, score in scores:
                name = await client.hget(score_key(message.chat.id) + ':names', user_id)
                if isinstance(name, bytes):
                    name = name.decode('utf-8', errors='replace')
                rows.append(f'{len(rows) + 1}. {escape(str(name or "Игрок")[:40])} — {int(score)}')
            return rows

        try:
            rows = await asyncio.wait_for(read(), timeout=5)
        except Exception:
            return await message.reply('Рейтинг сейчас недоступен.', request_timeout=SEND_TIMEOUT)
        text = '🏆 Рейтинг чата\n\n' + ('\n'.join(rows) if rows else 'Пока нет очков. Начни /geoguess')
        return await message.reply(text, parse_mode='HTML', request_timeout=SEND_TIMEOUT)

    @classmethod
    async def shutdown(cls):
        tasks = [round_.task for round_ in cls.rounds.values() if round_.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        cls.rounds.clear()
