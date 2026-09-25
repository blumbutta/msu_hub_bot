"""Painting quiz captions that reveal the artwork details only after voting."""

from collections.abc import Sequence
from dataclasses import dataclass

from aiogram.utils.formatting import Bold, Text, TextLink

from msu_hub_bot.commands.quiz_view import CAPTION_LIMIT, View, compact, user_label, user_label_size
from msu_hub_bot.providers.art import Artwork

# Leave space below Telegram's entity limit for the heading and museum link.
MAX_PLAYERS_PER_PAGE = 90


@dataclass(frozen=True)
class Player:
    user_id: int
    name: str
    username: str | None
    artist: str | None = None
    correct: bool = False


def _header(artwork: Artwork, players: Sequence[Player], *, closed: bool, scored: bool | None) -> Text:
    if not closed:
        return Text(
            Bold("🎨 Кто написал эту картину?"),
            f"\n🗳 Ответили: {len(players)}.\n",
            "Выбор каждого покажу в конце.\n",
            "Завершить может любой; автоматически — через 10 минут после картины.\n",
            "Верно: +1, ошибка: −1. Минимум за день — 0.",
        )
    summary = f"Угадали {sum(player.correct for player in players)} из {len(players)}." if players else "В этот раз никто не ответил."
    if scored is None:
        points = "Записываю очки…"
    elif scored:
        points = ""
    else:
        points = "Не удалось подтвердить запись очков."
    date = compact(artwork.date, 48)
    return Text(
        "🎨 Автор: ",
        Bold(compact(artwork.artist, 96)),
        "\n«",
        compact(artwork.title, 140),
        "»",
        f", {date}" if date else "",
        f".\n{summary}",
        f"\n{points}" if points else "",
    )


def _footer(artwork: Artwork, *, closed: bool, page: int, pages: int) -> Text:
    source = Text(TextLink("Картина на сайте музея", url=artwork.source_url)) if closed else Text()
    navigation = Text(f"Страница {page + 1}/{pages}") if pages > 1 else Text()
    return Text(source, "\n", navigation) if len(source) and len(navigation) else Text(source, navigation)


def _answer(player: Player) -> str:
    return compact(player.artist or "Неизвестно", 96)


def _pages(players: Sequence[Player], *, closed: bool, limit: int) -> list[tuple[int, int]]:
    """Pack whole rows by UTF-16 size without creating off-page user entities."""
    pages: list[tuple[int, int]] = []
    start = 0
    size = 0
    for index, player in enumerate(players):
        row_size = user_label_size(player.name, player.username)
        if closed:
            row_size += len(Text("✓ ", " — ", _answer(player)))
        separator = 1 if index > start else 0
        if index > start and (size + separator + row_size > limit or index - start >= MAX_PLAYERS_PER_PAGE):
            pages.append((start, index))
            start, size, separator = index, 0, 0
        size += separator + row_size
    pages.append((start, len(players)))
    return pages


def render(artwork: Artwork, players: Sequence[Player], *, closed: bool = False, scored: bool | None = True, page: int = 0) -> View:
    """Keep ordinary results together and paginate only overflowing participant rows."""
    header = _header(artwork, players, closed=closed, scored=scored)
    footer = _footer(artwork, closed=closed, page=0, pages=1)
    layouts = _pages(players, closed=closed, limit=CAPTION_LIMIT - len(header) - len(footer) - 4)
    if len(layouts) > 1:
        max_pages = max(1, len(players))
        footer = _footer(artwork, closed=closed, page=max_pages - 1, pages=max_pages)
        layouts = _pages(players, closed=closed, limit=CAPTION_LIMIT - len(header) - len(footer) - 4)
    pages = len(layouts)
    page = min(max(0, page), pages - 1)
    start, stop = layouts[page]
    rows: list[Text] = []
    for player in players[start:stop]:
        label = user_label(player.user_id, player.name, player.username)
        rows.append(Text("✓ " if player.correct else "✗ ", label, " — ", _answer(player)) if closed else label)
    participants = Text(*[Text(row, "\n" if index + 1 < len(rows) else "") for index, row in enumerate(rows)])
    if not players and not closed:
        participants = Text("Пока никто не ответил. Твой ход!")
    footer = _footer(artwork, closed=closed, page=page, pages=pages)
    body = Text(header, "\n\n", participants) if len(participants) else header
    caption, entities = Text(body, "\n\n" if len(footer) else "", footer).render()
    return View(caption, entities, page, pages)
