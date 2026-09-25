"""Painting quizzes with hidden votes and a daily chat leaderboard."""

import asyncio

from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Message

from msu_hub_bot.games.quiz import QuizService
from msu_hub_bot.telegram.context import bot_for


class ArtCallback(CallbackData, prefix="art"):
    round: str
    choice: str


class Art:
    callback_data = ArtCallback

    @staticmethod
    async def process(message: Message, quiz: QuizService) -> Message | None:
        return await quiz.start("art", message)

    @staticmethod
    async def process_cb(query: CallbackQuery, callback_data: ArtCallback, quiz: QuizService) -> bool | None:
        if not isinstance(query.message, Message):
            async with asyncio.timeout(15):
                return await bot_for(query)(query.answer("Этот раунд недоступен."), request_timeout=15)
        return await quiz.callback("art", query, callback_data.round, callback_data.choice)

    @staticmethod
    async def top(message: Message, quiz: QuizService) -> Message:
        try:
            async with asyncio.timeout(5):
                body = await quiz.ranking("art", message.chat.id)
            text, entities = body.render()
            method = message.reply(text, entities=entities, parse_mode=None)
        except Exception:
            method = message.reply("Рейтинг сейчас недоступен.")
        async with asyncio.timeout(15):
            return await bot_for(message)(method, request_timeout=15)
