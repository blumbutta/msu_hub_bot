"""One shared human chess match per chat, with durable clocks and global Elo."""

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass, field
from time import time
from typing import TypeVar

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.methods import EditMessageCaption, EditMessageMedia, TelegramMethod
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message, User
from aiogram.utils.formatting import Bold, Text

from msu_hub_bot.commands.chess_play_game import Game, GameError, Player
from msu_hub_bot.commands.chess_play_view import PlayCallback, keyboard, render
from msu_hub_bot.commands.quiz_view import user_label
from msu_hub_bot.media.chess_play_board import render_match
from msu_hub_bot.telegram import chess_play_rating as rating
from msu_hub_bot.telegram import chess_play_store as store
from msu_hub_bot.telegram.chess_play_store import SavedGame
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.runtime import AdmissionClosed, Supervisor
from msu_hub_bot.telegram.storage import RedisStorage

logger = logging.getLogger(__name__)
SEND_TIMEOUT = 10
CLOCK_INTERVAL = 5.0
RETRY_INTERVAL = 15.0
MAX_GAMES = 128
_Result = TypeVar("_Result")


async def _send(bot: Bot, method: TelegramMethod[_Result]) -> _Result:
    async with asyncio.timeout(SEND_TIMEOUT):
        return await bot(method, request_timeout=SEND_TIMEOUT)


def _player(user: User) -> Player:
    return Player(user_id=user.id, name=user.full_name, username=user.username)


def _image_key(game: Game) -> tuple[str, str, str | None, int | None, str]:
    return game.board().fen(), game.status, game.result, game.winner, " ".join(game.moves)


@dataclass
class Match:
    saved: SavedGame
    bot: Bot
    redis: RedisStorage
    supervisor: Supervisor
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    deadline_timer: asyncio.TimerHandle | None = None
    refresh_timer: asyncio.TimerHandle | None = None
    maintenance: asyncio.Task[None] | None = None
    delivery: asyncio.Task[None] | None = None
    dirty: bool = False
    last_image: tuple[str, str, str | None, int | None, str] | None = None
    last_edit: float = 0.0
    bootstrap: SavedGame | None = None


class ChessPlay:
    callback_data = PlayCallback
    matches: dict[tuple[int, int, str], Match] = {}
    closing = False

    @classmethod
    def open(cls) -> None:
        cls.closing = False

    @classmethod
    def _attach(cls, value: SavedGame, bot: Bot, redis: RedisStorage, supervisor: Supervisor) -> Match:
        game = value.game
        key = (bot.id, game.chat_id, game.token)
        existing = cls.matches.get(key)
        if existing is not None:
            return existing
        match = Match(value, bot, redis, supervisor)
        cls.matches[key] = match
        return match

    @classmethod
    async def _reload(cls, match: Match) -> bool:
        game = match.saved.game
        try:
            value = await store.load(match.redis, game.bot_id, game.chat_id, game.token)
        except ValueError:
            logger.warning("Discarding invalid in-memory chess match")
            value = None
        if value is None:
            cls._remove(match)
            return False
        match.saved = value
        return True

    @classmethod
    async def _commit(cls, match: Match, value: SavedGame) -> None:
        await store.save(match.redis, value, match.saved)
        match.saved = value
        cls._arm(match)

    @classmethod
    def _remove(cls, match: Match) -> None:
        for timer in (match.deadline_timer, match.refresh_timer):
            if timer is not None:
                timer.cancel()
        game = match.saved.game
        key = (game.bot_id, game.chat_id, game.token)
        if cls.matches.get(key) is match:
            cls.matches.pop(key)

    @classmethod
    def _arm(cls, match: Match) -> None:
        if match.deadline_timer is not None:
            match.deadline_timer.cancel()
            match.deadline_timer = None
        if cls.closing:
            return
        deadline = match.saved.game.deadline()
        if deadline is not None:
            match.deadline_timer = asyncio.get_running_loop().call_later(max(0, deadline - time()), cls._wake, match)
        if match.refresh_timer is None:
            cls._refresh_later(match)

    @classmethod
    def _refresh_later(cls, match: Match, delay: float = CLOCK_INTERVAL) -> None:
        if cls.closing:
            return
        if match.refresh_timer is not None:
            match.refresh_timer.cancel()
        match.refresh_timer = asyncio.get_running_loop().call_later(delay, cls._refresh, match)

    @classmethod
    def _refresh(cls, match: Match) -> None:
        match.refresh_timer = None
        cls._wake(match)
        cls._show(match)
        cls._refresh_later(match)

    @classmethod
    def _wake(cls, match: Match) -> None:
        if cls.closing or (match.maintenance is not None and not match.maintenance.done()):
            return
        try:
            match.maintenance = match.supervisor.create_job(lambda: cls._maintain(match))
        except AdmissionClosed:
            pass

    @classmethod
    async def _maintain(cls, match: Match) -> None:
        """Clock adjudication never waits for Telegram or board rendering."""
        try:
            async with match.lock:
                if match.bootstrap is not None:
                    await cls._commit(match, match.bootstrap)
                    match.bootstrap = None
                if not await cls._reload(match):
                    return
                value = match.saved.model_copy(deep=True)
                if value.game.message_id is None:
                    # The Telegram send and its metadata commit may still be in
                    # flight. Do not mistake another handler's live creation for
                    # a crash; abandoned reservations are bounded to one minute.
                    if time() < value.game.created_at + 60:
                        cls._arm(match)
                        return
                    # Interrupted creation has no safely addressable board.
                    value.game.cancel(value.game.white.user_id, min(time(), value.game.invite_deadline - 0.001))
                    value.revealed = True
                    await cls._commit(match, value)
                elif value.game.expire(time()):
                    await cls._commit(match, value)
                cls._arm(match)
                if match.saved.game.status == "finished" and match.saved.game.black is not None and match.saved.ratings is None:
                    changes = await rating.settle(match.saved.game, match.redis)
                    value = match.saved.model_copy(deep=True)
                    value.ratings = changes
                    await cls._commit(match, value)
                if match.saved.revealed:
                    cls._remove(match)
                    return
            cls._show(match)
        except Exception:
            logger.exception("Chess match maintenance failed")
            # An expired deadline must not create a hot retry loop.
            if match.deadline_timer is not None:
                match.deadline_timer.cancel()
                match.deadline_timer = None
            cls._refresh_later(match, RETRY_INTERVAL)

    @classmethod
    def _show(cls, match: Match) -> None:
        if cls.closing or match.saved.game.message_id is None:
            return
        match.dirty = True
        if match.delivery is not None and not match.delivery.done():
            return
        try:
            match.delivery = match.supervisor.create_job(lambda: cls._deliver(match))
        except AdmissionClosed:
            pass

    @classmethod
    async def _deliver(cls, match: Match) -> None:
        try:
            while match.dirty and not cls.closing:
                match.dirty = False
                # Coalesce fast taps; clock refreshes use captions, not new PNGs.
                await asyncio.sleep(max(0, match.last_edit + 1 - asyncio.get_running_loop().time()))
                snapshot = match.saved.model_copy(deep=True)
                game = snapshot.game
                if game.message_id is None:
                    return
                view = render(game, time(), ratings=snapshot.ratings)
                markup = keyboard(game)
                image_key = _image_key(game)
                try:
                    if match.last_image != image_key:
                        photo = await asyncio.to_thread(render_match, game)
                        await _send(
                            match.bot,
                            EditMessageMedia(
                                chat_id=game.chat_id,
                                message_id=game.message_id,
                                media=InputMediaPhoto(
                                    media=BufferedInputFile(photo, filename="chess.png"),
                                    caption=view.caption,
                                    caption_entities=view.entities,
                                    parse_mode=None,
                                ),
                                reply_markup=markup,
                            ),
                        )
                        match.last_image = image_key
                    else:
                        await _send(
                            match.bot,
                            EditMessageCaption(
                                chat_id=game.chat_id,
                                message_id=game.message_id,
                                caption=view.caption,
                                caption_entities=view.entities,
                                parse_mode=None,
                                reply_markup=markup,
                            ),
                        )
                except TelegramBadRequest as exc:
                    if "message is not modified" not in exc.message.lower():
                        raise
                match.last_edit = asyncio.get_running_loop().time()
                async with match.lock:
                    if match.saved != snapshot:
                        match.dirty = True
                    elif game.status == "finished" and (game.black is None or snapshot.ratings is not None):
                        value = snapshot.model_copy(deep=True)
                        value.revealed = True
                        await cls._commit(match, value)
                        cls._remove(match)
                        return
        except Exception:
            logger.exception("Chess board delivery failed")
            cls._refresh_later(match, RETRY_INTERVAL)

    @classmethod
    async def process(cls, message: Message, redis: RedisStorage, supervisor: Supervisor) -> Message | None:
        bot = bot_for(message)
        if message.chat.type not in {"group", "supergroup"} or message.from_user is None or message.from_user.is_bot:
            return await _send(bot, message.reply("Начни партию от своего имени в групповом чате."))
        if cls.closing:
            return None
        match: Match | None = None
        created = False
        try:
            active = await store.active(redis, bot.id, message.chat.id)
            if active is not None:
                match = cls._attach(active, bot, redis, supervisor)
                await cls._maintain(match)
                if await store.active(redis, bot.id, message.chat.id) is not None:
                    return await _send(bot, message.reply("Подождите, текущая партия ещё не завершена!"))
            if len(cls.matches) >= MAX_GAMES:
                return await _send(bot, message.reply("Сейчас слишком много партий. Попробуй немного позже."))
            now = time()
            creator_rating = await rating.get_player(bot.id, message.from_user.id, redis)
            value = SavedGame(
                game=Game(
                    token=secrets.token_hex(6),
                    bot_id=bot.id,
                    chat_id=message.chat.id,
                    thread_id=message.message_thread_id,
                    white=_player(message.from_user),
                    white_rating=creator_rating.rating,
                    created_at=now,
                    invite_deadline=now + 600,
                )
            )
            match = cls._attach(value, bot, redis, supervisor)
            async with match.lock:
                await store.save(redis, value)
                created = True
                view = render(value.game, now)
                photo = await asyncio.to_thread(render_match, value.game)
                sent = await _send(
                    bot,
                    message.reply_photo(
                        BufferedInputFile(photo, filename="chess.png"),
                        caption=view.caption,
                        caption_entities=view.entities,
                        parse_mode=None,
                    ),
                )
                candidate = value.model_copy(deep=True)
                candidate.game.message_id = sent.message_id
                match.bootstrap = candidate
                # Retry this idempotent metadata write once: never resend a photo
                # after an ambiguous Telegram response.
                try:
                    await cls._commit(match, candidate)
                except Exception:
                    await cls._commit(match, candidate)
                match.bootstrap = None
                match.last_image = _image_key(candidate.game)
                match.last_edit = asyncio.get_running_loop().time()
                cls._arm(match)
                cls._show(match)
        except store.Conflict:
            if match is not None:
                if created:
                    cls._wake(match)
                else:
                    cls._remove(match)
            return await _send(bot, message.reply("Подождите, текущая партия ещё не завершена!"))
        except Exception:
            logger.exception("Chess match creation failed")
            if match is not None:
                cls._wake(match)
            return await _send(bot, message.reply("Не удалось открыть партию. Попробуйте ещё раз."))
        return None

    @staticmethod
    async def _apply(game: Game, data: PlayCallback, user: User, now: float, redis: RedisStorage) -> None:
        uid = user.id
        if data.action == "join":
            # Validate the claim before touching rating records.
            game.join(_player(user), now)
            assert game.black is not None
            game.white_rating, game.black_rating = await rating.start_pair(game.bot_id, game.white, game.black, redis)
            game.turn_started = time()
        elif data.action == "pick":
            game.select(uid, data.value, now)
        elif data.action == "back":
            game.select(uid, None, now)
        elif data.action == "move":
            game.move(uid, data.value, now)
        elif data.action == "cancel":
            game.cancel(uid, now)
        elif data.action == "resign":
            game.resign(uid, now)
        elif data.action == "draw":
            game.offer_draw(uid, now)
        elif data.action == "accept_draw":
            game.accept_draw(uid, now)
        elif data.action == "decline_draw":
            game.decline_draw(uid, now)
        elif data.action == "claim_draw":
            game.claim_draw(uid, now)
        else:
            raise GameError("Эта кнопка больше не действует.")

    @classmethod
    async def callback(cls, query: CallbackQuery, callback_data: PlayCallback, redis: RedisStorage, supervisor: Supervisor) -> None:
        if not isinstance(query.message, Message):
            await query.answer("Доска недоступна.")
            return
        bot = bot_for(query)
        data = callback_data
        answer = ""
        match: Match | None = None
        try:
            if cls.closing or re.fullmatch(r"[0-9a-f]{12}", data.game) is None:
                raise GameError("Партия недоступна.")
            saved = await store.load(redis, bot.id, query.message.chat.id, data.game)
            if saved is None or saved.game.message_id != query.message.message_id:
                raise GameError("Эта партия уже недоступна.")
            match = cls._attach(saved, bot, redis, supervisor)
            async with match.lock:
                if not await cls._reload(match):
                    raise GameError("Эта партия уже недоступна.")
                value = match.saved.model_copy(deep=True)
                game = value.game
                if game.expire(time()):
                    await cls._commit(match, value)
                if game.status == "finished":
                    raise GameError("Партия уже завершена.")
                if game.black is not None and query.from_user.id not in {game.white.user_id, game.black.user_id}:
                    raise GameError("Вы наблюдаете за партией.")
                if data.revision != game.revision:
                    raise GameError("Доска обновилась. Нажми кнопку ещё раз.")
                await cls._apply(game, data, query.from_user, time(), redis)
                await cls._commit(match, value)
        except GameError as exc:
            answer = str(exc)
        except Exception:
            logger.exception("Chess action could not be confirmed")
            answer = "Не удалось подтвердить действие. Проверь доску и попробуй ещё раз."
        finally:
            if match is not None:
                cls._wake(match)
                cls._show(match)
            try:
                await _send(bot, query.answer(answer))
            except TelegramAPIError, TimeoutError:
                pass

    @classmethod
    async def restore(cls, bot: Bot, redis: RedisStorage, supervisor: Supervisor) -> None:
        if cls.closing:
            return
        for value in await store.pending(redis, bot.id):
            match = cls._attach(value, bot, redis, supervisor)
            cls._wake(match)

    @classmethod
    async def close(cls) -> None:
        cls.closing = True
        tasks: list[asyncio.Task[None]] = []
        for match in tuple(cls.matches.values()):
            cls._remove(match)
            tasks.extend(task for task in (match.maintenance, match.delivery) if task is not None and not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class RatingCallback(CallbackData, prefix="chrate"):
    page: int


class ChessRating:
    callback_data = RatingCallback

    @staticmethod
    async def _view(bot: Bot, redis: RedisStorage, user: User, page: int = 0) -> tuple[Text, InlineKeyboardMarkup | None]:
        board = await rating.leaderboard(bot.id, redis, page=max(0, page))
        person = await rating.get_player(bot.id, user.id, redis)
        rows: list[Text | str] = [Bold("♟ Общий шахматный рейтинг"), "\nЗа всё время · старт 800 · Elo\n"]
        for index, player in enumerate(board.players, board.page * 10 + 1):
            rows.append(Text(f"\n{index}. ", user_label(player.user_id, player.name, player.username), f" — {player.rating}"))
        if not board.players:
            rows.append("\nПока никто не сыграл. Начни с /chess_play.")
        rows.append(
            Text(
                "\n\n",
                user_label(user.id, user.full_name, user.username),
                f": {person.rating}",
                f" · место {person.rank}" if person.rank is not None else " · ещё нет партий",
            )
        )
        markup = None
        if board.pages > 1:
            rows.append(f"\nСтраница {board.page + 1}/{board.pages}")
            buttons = [
                InlineKeyboardButton(text=label, callback_data=RatingCallback(page=number).pack())
                for label, number in (("‹", board.page - 1), ("›", board.page + 1))
                if 0 <= number < board.pages
            ]
            markup = InlineKeyboardMarkup(inline_keyboard=[buttons])
        return Text(*rows), markup

    @classmethod
    async def process(cls, message: Message, redis: RedisStorage) -> Message | None:
        if message.from_user is None:
            return None
        bot = bot_for(message)
        try:
            text, markup = await cls._view(bot, redis, message.from_user)
            return await _send(bot, message.reply(**text.as_kwargs(), reply_markup=markup))
        except Exception:
            logger.exception("Chess ratings unavailable")
            return await _send(bot, message.reply("Не удалось получить рейтинг. Попробуйте ещё раз."))

    @classmethod
    async def callback(cls, query: CallbackQuery, callback_data: RatingCallback, redis: RedisStorage) -> None:
        if not isinstance(query.message, Message):
            await query.answer("Сообщение недоступно.")
            return
        bot = bot_for(query)
        try:
            text, markup = await cls._view(bot, redis, query.from_user, callback_data.page)
            await _send(bot, query.message.edit_text(**text.as_kwargs(), reply_markup=markup))
            await _send(bot, query.answer())
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message.lower():
                raise
            await _send(bot, query.answer())
