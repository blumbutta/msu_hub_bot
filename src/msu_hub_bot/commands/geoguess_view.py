"""Bounded photo captions for a GeoGuess round and its participant pages."""

from collections.abc import Sequence
from dataclasses import dataclass

from aiogram.types import MessageEntity
from aiogram.utils.formatting import Bold, Text, TextLink

from msu_hub_bot.providers.geoguess import COUNTRIES, Photo

CAPTION_LIMIT = 1024
PAGE_SIZE = 4
COUNTRY_CODES = {name: code for code, name in COUNTRIES.items()}


@dataclass(frozen=True)
class Player:
    user_id: int
    name: str
    username: str | None
    country: str | None = None
    correct: bool = False


@dataclass(frozen=True)
class View:
    caption: str
    entities: list[MessageEntity]
    page: int
    pages: int


def compact(value: str, limit: int) -> str:
    """Flatten whitespace and shorten without splitting an emoji's UTF-16 pair."""
    if limit < 0:
        raise ValueError("Text limit must be non-negative")
    value = " ".join(value.split())
    if len(Text(value)) <= limit:
        return value
    remaining = max(0, limit - 1)
    prefix: list[str] = []
    for char in value:
        width = 2 if ord(char) > 0xFFFF else 1
        if width > remaining:
            break
        prefix.append(char)
        remaining -= width
    return "".join(prefix).rstrip() + ("…" if limit else "")


def country_label(country: str) -> str:
    code = COUNTRY_CODES.get(country)
    if code is None:
        return country
    flag = "".join(chr(0x1F1E6 + ord(letter) - ord("a")) for letter in code)
    return f"{flag} {country}"


def user_label(user_id: int, name: str, username: str | None) -> Text:
    label = Text(TextLink(compact(name, 48) or "Игрок", url=f"tg://user?id={user_id}"))
    handle = compact(username.lstrip("@"), 32) if username else ""
    return Text(label, f" (@{handle})") if handle else label


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
    credit = Text("Фото: ", compact(photo.author, 48), ", ", TextLink(compact(photo.license, 32), url=photo.license_url), ".")
    source = (
        Text(
            "\n",
            TextLink("Источник фотографии", url=photo.source),
            " · ",
            TextLink("© OpenStreetMap", url="https://www.openstreetmap.org/copyright"),
        )
        if closed
        else Text()
    )
    navigation = f"\nСтраница {page + 1}/{pages}" if pages > 1 else ""
    return Text(credit, source, navigation)


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
    caption, entities = Text(body, "\n\n", footer).render()
    return View(caption, entities, page, pages)
