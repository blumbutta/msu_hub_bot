"""Chess questions, continuations and answer pages within one photo caption."""

from collections.abc import Sequence
from dataclasses import dataclass

from aiogram.utils.formatting import Bold, Code, Text, TextLink

from msu_hub_bot.commands.quiz_view import CAPTION_LIMIT, PAGE_SIZE, View, compact, user_label
from msu_hub_bot.providers.chess import Puzzle


@dataclass(frozen=True)
class Player:
    user_id: int
    name: str
    username: str | None
    move: str | None = None
    correct: bool = False


def _header(puzzle: Puzzle, players: Sequence[Player], *, closed: bool, scored: bool | None) -> Text:
    if not closed:
        side = "белых" if puzzle.fen.split()[1] == "w" else "чёрных"
        return Text(
            Bold(f"♟ Ход {side}. Найди лучший ход."),
            f"\n🗳 Ответили: {len(players)}.\n",
            "Выбор каждого покажу в конце.\n",
            "Завершить может любой; автоматически — через 10 минут после появления доски.\n",
            "Верно: +1, ошибка: −1. Минимум за день — 0.",
        )
    answer = next(option.label for option in puzzle.options if option.uci == puzzle.solution[0])
    summary = f"Угадали {sum(player.correct for player in players)} из {len(players)}." if players else "В этот раз никто не ответил."
    if scored is None:
        points = "Записываю очки…"
    elif scored:
        points = ""
    else:
        points = "Не удалось подтвердить запись очков."
    return Text("♟ Правильный ход: ", Bold(compact(answer, 80)), f".\n{summary}", f"\n{points}" if points else "")


def _solution_turns(puzzle: Puzzle) -> list[str]:
    """Keep every SAN move and its number, including a black first move."""
    move_number = int(puzzle.fen.split()[5])
    white = puzzle.fen.split()[1] == "w"
    turns: list[str] = []
    for san in puzzle.line:
        if white or not turns:
            turns.append(f"{move_number}{'.' if white else '...'} {san}")
        else:
            turns[-1] += f" {san}"
        if not white:
            move_number += 1
        white = not white
    return turns


def _result_bodies(puzzle: Puzzle, players: Sequence[Player], *, scored: bool | None, limit: int) -> list[Text]:
    """Pack the continuation and answers together; paginate only actual overflow."""
    bodies: list[Text] = []
    continuation = ""
    for turn in _solution_turns(puzzle):
        candidate = f"{continuation} {turn}" if continuation else turn
        if continuation and len(Text("Продолжение:\n", Code(candidate))) > limit:
            bodies.append(Text("Продолжение:\n", Code(continuation)))
            continuation = turn
        else:
            continuation = candidate
    body = Text("Продолжение:\n", Code(continuation))
    has_players = False
    for player in players:
        result = ("✓ +1 " if player.correct else "✗ −1 ") if scored else ("✓ " if player.correct else "✗ ")
        row = Text(
            result,
            user_label(player.user_id, player.name, player.username),
            " — ",
            compact(player.move or "Неизвестный ход", 80),
        )
        candidate_body = Text(body, "\n" if has_players else "\n\nОтветы:\n", row)
        if len(candidate_body) > limit:
            bodies.append(body)
            body = Text("Ответы:\n", row)
        else:
            body = candidate_body
        has_players = True
    bodies.append(body)
    return bodies


def _footer(puzzle: Puzzle, *, closed: bool, page: int, pages: int) -> Text:
    source = Text(TextLink("Задача на Lichess", url=f"https://lichess.org/training/{puzzle.id}")) if closed else Text()
    navigation = Text(f"Страница {page + 1}/{pages}") if pages > 1 else Text()
    return Text(source, "\n", navigation) if len(source) and len(navigation) else Text(source, navigation)


def render(puzzle: Puzzle, players: Sequence[Player], *, closed: bool, scored: bool | None = True, page: int = 0) -> View:
    """Keep normal results on one page and retain all answers when they overflow."""
    header = _header(puzzle, players, closed=closed, scored=scored)
    if closed:
        footer_budget = len(_footer(puzzle, closed=True, page=0, pages=1))
        limit = CAPTION_LIMIT - len(header) - footer_budget - 4
        bodies = _result_bodies(puzzle, players, scored=scored, limit=limit)
        if len(bodies) > 1:
            # Account for navigation only after the complete result really overflows.
            max_pages = max(1, len(players) + len(puzzle.line))
            footer_budget = len(_footer(puzzle, closed=True, page=max_pages - 1, pages=max_pages))
            bodies = _result_bodies(puzzle, players, scored=scored, limit=CAPTION_LIMIT - len(header) - footer_budget - 4)
        pages = len(bodies)
        page = min(max(0, page), pages - 1)
        body = bodies[page]
    else:
        pages = max(1, (len(players) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(0, page), pages - 1)
        start = page * PAGE_SIZE
        selected = players[start : start + PAGE_SIZE]
        rows = [user_label(player.user_id, player.name, player.username) for player in selected]
        body = Text(*[Text(row, "\n" if index + 1 < len(rows) else "") for index, row in enumerate(rows)])
        if not players:
            body = Text("Пока никто не ответил. Твой ход!")
    footer = _footer(puzzle, closed=closed, page=page, pages=pages)
    text = Text(header, "\n\n", body)
    if len(footer):
        text = Text(text, "\n\n", footer)
    caption, entities = text.render()
    return View(caption, entities, page, pages)
