"""Cooperative branching stories with a vote deadline started by the first player."""

import asyncio
import logging

from aiogram.types import CallbackQuery, Message

from msu_hub_bot.commands.quest_view import QuestCallback
from msu_hub_bot.games.quest import QuestService
from msu_hub_bot.providers.quest import DEFAULT_STORY_ID, QuestError
from msu_hub_bot.telegram.context import bot_for
from msu_hub_bot.telegram.filters import MetaInfo

logger = logging.getLogger(__name__)


class Quest:
    callback_data = QuestCallback

    @staticmethod
    async def process(message: Message, meta: MetaInfo, quests: QuestService) -> Message | None:
        if len(meta.arguments) > 1:
            answer = "Укажи один квест: /quest или /quest ID. Для примера — /quest demo."
        else:
            story_id = meta.arguments[0] if meta.arguments else DEFAULT_STORY_ID
            try:
                return await quests.start(message, story_id)
            except QuestError:
                answer = "Не удалось открыть этот квест: он недоступен или его механика пока не поддерживается. Попробуй /quest demo."
            except Exception:
                logger.exception("Quest unavailable")
                answer = "Не удалось открыть квест. Попробуй чуть позже."
        async with asyncio.timeout(15):
            return await bot_for(message)(message.reply(answer, parse_mode=None), request_timeout=15)

    @staticmethod
    async def callback(query: CallbackQuery, callback_data: QuestCallback, quests: QuestService) -> None:
        await quests.callback(query, callback_data)
