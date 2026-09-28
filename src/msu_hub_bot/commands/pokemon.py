"""Pokémon silhouette quizzes with hidden votes and a daily chat leaderboard."""

import asyncio

from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Message

from msu_hub_bot.games.quiz import QuizService
from msu_hub_bot.telegram.context import bot_for


class PokemonCallback(CallbackData, prefix="pokemon"):
    round: str
    choice: str


class Pokemon:
    callback_data = PokemonCallback

    @staticmethod
    async def process(message: Message, quiz: QuizService) -> Message | None:
        return await quiz.start("pokemon", message)

    @staticmethod
    async def process_cb(query: CallbackQuery, callback_data: PokemonCallback, quiz: QuizService) -> bool | None:
        if not isinstance(query.message, Message):
            async with asyncio.timeout(15):
                return await bot_for(query)(query.answer("Этот раунд недоступен."), request_timeout=15)
        return await quiz.callback("pokemon", query, callback_data.round, callback_data.choice)

    @staticmethod
    async def top(message: Message, quiz: QuizService) -> Message:
        try:
            async with asyncio.timeout(5):
                body = await quiz.ranking("pokemon", message.chat.id)
            text, entities = body.render()
            method = message.reply(text, entities=entities, parse_mode=None)
        except Exception:
            method = message.reply("Рейтинг сейчас недоступен.")
        async with asyncio.timeout(15):
            return await bot_for(message)(method, request_timeout=15)
