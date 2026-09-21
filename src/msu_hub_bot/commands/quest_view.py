"""Plain, paginated story text with one shared set of choice buttons."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pydantic import Field

from msu_hub_bot.commands.quiz_view import compact
from msu_hub_bot.providers.quest import QuestScene

TEXT_LIMIT = 4096
BODY_LIMIT = 2800
MOSCOW = ZoneInfo("Europe/Moscow")


class QuestCallback(CallbackData, prefix="quest"):
    game_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,32}$")
    scene_version: int = Field(ge=0, le=999_999)
    action: Literal["vote", "finish", "page"]
    value: str = Field(default="", pattern=r"^(?:0|[1-9][0-9]{0,5})?$")


@dataclass(frozen=True)
class QuestView:
    text: str
    keyboard: InlineKeyboardMarkup | None
    page: int = 0
    pages: int = 1


def _pages(text: str) -> list[str]:
    """Preserve all story text while respecting Telegram's UTF-16 limit."""
    result: list[str] = []
    start = 0
    width = 0
    for index, char in enumerate(text):
        size = 2 if ord(char) > 0xFFFF else 1
        if width + size > BODY_LIMIT:
            result.append(text[start:index])
            start = index
            width = 0
        width += size
    result.append(text[start:])
    return result


def render(
    scene: QuestScene,
    *,
    title: str,
    token: str,
    step: int,
    counts: list[int],
    voters: list[str],
    deadline: datetime | None,
    finished: bool,
    last_choice: str | None,
    page: int = 0,
    author: str = "",
) -> QuestView:
    """Counts stay private during voting; the next scene announces the decision."""
    heading = f"{'🏁' if finished else '🧭'} {compact(title, 120)}"
    if author:
        heading += f"\nАвтор: {compact(author, 100)}"
    heading += f"\n{'Квест завершён' if finished else f'Сцена {step + 1}'}"
    if last_choice:
        heading += f"\nПрошлый выбор: {compact(last_choice, 160)}"
    text = scene.text
    if scene.choices and not finished:
        text += "\n\nЧто делаем?\n" + "\n".join(f"{index + 1}. {label}" for index, label in enumerate(scene.choices))
    if voters and not finished:
        text += "\n\nУже проголосовали: " + ", ".join(voters)
    parts = _pages(text)
    page = min(max(0, page), len(parts) - 1)
    if finished:
        status = "Начать новую историю: /quest"
    elif not scene.choices:
        status = "Обновляю сцену…"
    elif deadline is None:
        status = "🗳 Голосов: 0.\nЖдём первый голос — он запустит 10 минут на выбор."
    else:
        until = deadline.astimezone(MOSCOW).strftime("%H:%M:%S")
        status = (
            f"🗳 Голосов: {sum(counts)}. Выбор можно изменить.\n"
            f"⏳ До {until} МСК. Завершить досрочно может любой.\n"
            "При равенстве — случайный вариант среди лидеров."
        )
    if len(parts) > 1:
        status += f"\nСтраница {page + 1}/{len(parts)}"
    rows: list[list[InlineKeyboardButton]] = []

    def button(label: str, action: Literal["vote", "finish", "page"], value: str = "") -> InlineKeyboardButton:
        data = QuestCallback(game_id=token, scene_version=step, action=action, value=value)
        return InlineKeyboardButton(text=label, callback_data=data.pack())

    if not finished and scene.choices:
        for index, label in enumerate(scene.choices):
            # Full choices remain in the paginated text, even when a long label
            # would be clipped by a Telegram client on a narrow screen.
            number = index + 1
            label = f"{number}. {label}" if len(label) <= 56 else f"Выбрать вариант {number}"
            rows.append([button(label, "vote", str(index))])
        rows.append([button("⏭ Завершить выбор досрочно", "finish")])
    if len(parts) > 1:
        rows.append(
            [
                button(label, "page", str(target))
                for label, target in (("← Назад", page - 1), ("Далее →", page + 1))
                if 0 <= target < len(parts)
            ]
        )
    return QuestView(
        text=f"{heading}\n\n{parts[page]}\n\n{status}",
        keyboard=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
        page=page,
        pages=len(parts),
    )
