"""Bounded photo captions for a GeoGuess round and its participant pages."""

from collections.abc import Sequence
from dataclasses import dataclass

from aiogram.utils.formatting import Bold, Text, TextLink

from msu_hub_bot.commands.quiz_view import CAPTION_LIMIT as CAPTION_LIMIT
from msu_hub_bot.commands.quiz_view import PAGE_SIZE as PAGE_SIZE
from msu_hub_bot.commands.quiz_view import View as View
from msu_hub_bot.commands.quiz_view import compact as compact
from msu_hub_bot.commands.quiz_view import user_label as user_label
from msu_hub_bot.providers.geoguess import COUNTRIES, Photo

COUNTRY_CODES = {name: code for code, name in COUNTRIES.items()}


@dataclass(frozen=True)
class Player:
    user_id: int
    name: str
    username: str | None
    country: str | None = None
    correct: bool = False


def country_label(country: str) -> str:
    code = COUNTRY_CODES.get(country)
    if code is None:
        return country
    flag = "".join(chr(0x1F1E6 + ord(letter) - ord("a")) for letter in code)
    return f"{flag} {country}"


def _header(photo: Photo, players: Sequence[Player], *, closed: bool, scored: bool | None) -> Text:
    if not closed:
        return Text(
            Bold("🌍 Угадай страну"),
            f"\n🗳 Ответили: {len(players)}.\n",
            "Выбор каждого покажу в конце.\n",
            "Завершить может любой; автоматически — через 10 минут после фото.",
        )
    place = ", ".join(part for part in (compact(photo.city, 60), compact(country_label(photo.country), 70)) if part)
    summary = f"Угадали {sum(player.correct for player in players)} из {len(players)}." if players else "В этот раз никто не ответил."
    if scored is None:
        points = "Записываю очки…"
    elif scored:
        points = "Верно: +1, ошибка: −1. Минимум за день — 0."
    else:
        points = "Не удалось подтвердить запись очков."
    return Text("🌍 На снимке — ", Bold(place), f".\n{summary}\n{points}")


def _footer(photo: Photo, *, closed: bool, page: int, pages: int) -> Text:
    navigation = f"Страница {page + 1}/{pages}" if pages > 1 else ""
    if not closed:
        return Text(navigation)
    credit = Text("Фото: ", compact(photo.author, 48), ", ", TextLink(compact(photo.license, 32), url=photo.license_url), ".")
    source = Text(
        "\n",
        TextLink("Источник фотографии", url=photo.source),
        " · ",
        TextLink("© OpenStreetMap", url="https://www.openstreetmap.org/copyright"),
    )
    return Text(credit, source, "\n" if navigation else "", navigation)


def render(photo: Photo, players: Sequence[Player], *, closed: bool, scored: bool | None = True, page: int = 0) -> View:
    """Render one fixed-size page; only the selected participants need formatting."""
    pages = max(1, (len(players) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    header = _header(photo, players, closed=closed, scored=scored)
    footer = _footer(photo, closed=closed, page=page, pages=pages)
    selected = players[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    # Reserve separators first, then share the remaining UTF-16 budget per row.
    row_budget = (CAPTION_LIMIT - len(header) - len(footer) - 4) // max(1, len(selected)) - 1
    rows: list[Text] = []
    for player in selected:
        label = user_label(player.user_id, player.name, player.username)
        if closed:
            country_budget = min(70, row_budget - len(label) - 5)
            country = compact(country_label(player.country or "Неизвестно"), country_budget)
            rows.append(Text("✓ " if player.correct else "✗ ", label, " — ", country))
        else:
            rows.append(label)
    participants = Text(*[Text(row, "\n" if index + 1 < len(rows) else "") for index, row in enumerate(rows)])
    if not players and not closed:
        participants = Text("Пока никто не ответил. Твой ход!")
    body = Text(header, "\n\n", participants) if len(participants) else header
    caption, entities = (Text(body, "\n\n", footer) if len(footer) else body).render()
    return View(caption, entities, page, pages)
