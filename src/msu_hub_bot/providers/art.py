"""Bounded painting questions and CC0 images from the Cleveland Museum of Art."""

import asyncio
import io
import json
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit

import aiohttp
from PIL import Image

from msu_hub_bot.providers.exceptions import ExternalServiceError

API_URL = "https://openaccess-api.clevelandart.org/api/artworks/"
IMAGE_HOST = "openaccess-cdn.clevelandart.org"
USER_AGENT = "MSUHubBot-Art/1.0 (https://github.com/uburuntu/msu_hub_bot)"
FETCH_TIMEOUT = 16.0
IMAGE_TIMEOUT = 8.0
MAX_RESPONSE_BYTES = 512_000
MAX_IMAGE_BYTES = 5_000_000
MAX_IMAGE_PIXELS = 12_000_000
PAGE_SIZE = 40
MAX_ATTEMPTS = 5
REQUEST_INTERVAL = 1.0
RATE_LIMIT_COOLDOWN = 60.0
FIELDS = "id,accession_number,title,creators,creation_date,images,url,share_license_status,type"
_request_lock: asyncio.Lock | None = None
_next_request_at = 0.0
_cooldown_until = 0.0
_AMBIGUOUS = re.compile(
    r"\b(?:attributed|workshop|school of|studio of|circle of|follower|manner of|style of|"
    r"after(?=\s+[^\W\d_])|possibly|probably|anonymous|unknown|unidentified|copy of|copy after|imitator)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Artwork:
    id: str
    title: str
    artist: str
    date: str
    image_url: str
    source_url: str


@dataclass(frozen=True)
class ArtPuzzle:
    artwork: Artwork
    options: tuple[str, ...]
    answer: int


class UnsuitableArtwork(ValueError):
    """An artwork or response cannot establish a unique, named painter."""


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise UnsuitableArtwork("Expected an object")
    return cast(dict[str, object], value)


def _items(value: object, maximum: int) -> list[object]:
    if not isinstance(value, list) or len(value) > maximum:
        raise UnsuitableArtwork("Invalid result list")
    return cast(list[object], value)


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise UnsuitableArtwork("Missing or oversized text")
    if any(unicodedata.category(char) in {"Cc", "Cf"} and char not in "\n\r\t" for char in value):
        raise UnsuitableArtwork("Unsupported text controls")
    return " ".join(value.split())


def _identifier(value: object) -> int:
    if type(value) is not int or not 0 < value < 2**63:
        raise UnsuitableArtwork("Invalid identifier")
    return value


def _label_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _image_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 500:
        raise UnsuitableArtwork("Invalid image URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != IMAGE_HOST or parsed.query or parsed.fragment:
        raise UnsuitableArtwork("Untrusted image origin")
    # Only the documented web-sized JPEG asset, never print/TIFF or an arbitrary
    # provider URL. The path contains an accession number, not an artist/title.
    if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*_web\.jpg", parsed.path):
        raise UnsuitableArtwork("Invalid image asset")
    if ".." in parsed.path:
        raise UnsuitableArtwork("Invalid image path")
    return value


def _has_lifespan(creator: dict[str, object]) -> bool:
    for year in (creator.get("birth_year"), creator.get("death_year")):
        if isinstance(year, str) and re.fullmatch(r"-?[0-9]{1,4}", year):
            if -5000 <= int(year) <= 3000 and int(year) != 0:
                return True
    return False


def parse_artwork(value: object) -> tuple[Artwork, int]:
    record = _object(value)
    identifier = _identifier(record.get("id"))
    if record.get("share_license_status") != "CC0" or record.get("type") != "Painting":
        raise UnsuitableArtwork("Only CC0 paintings are supported")
    creators = _items(record.get("creators"), 100)
    if len(creators) != 1:
        raise UnsuitableArtwork("Artwork must have exactly one creator")
    creator = _object(creators[0])
    artist_id = _identifier(creator.get("id"))
    if creator.get("role") not in {"artist", "painter"} or not _has_lifespan(creator):
        raise UnsuitableArtwork("Missing a named individual painter")
    if creator.get("qualifier") not in (None, "") or creator.get("extent") not in (None, ""):
        raise UnsuitableArtwork("Qualified or partial authorship")
    description = _text(creator.get("description"), 500)
    if _AMBIGUOUS.search(description):
        raise UnsuitableArtwork("Uncertain attribution")
    # The museum's display field appends nationality/lifespan in parentheses.
    # Preserve the original spelling; do not guess a Russian translation.
    artist = _text(description.split(" (", 1)[0], 120)
    accession = record.get("accession_number")
    if not isinstance(accession, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", accession) or ".." in accession:
        raise UnsuitableArtwork("Invalid accession number")
    date = record.get("creation_date")
    web_image = _object(_object(record.get("images")).get("web"))
    return (
        Artwork(
            id=str(identifier),
            title=_text(record.get("title"), 500),
            artist=artist,
            date="" if date is None else _text(date, 250),
            image_url=_image_url(web_image.get("url")),
            source_url=f"https://www.clevelandart.org/art/{accession}",
        ),
        artist_id,
    )


async def _request_json(session: aiohttp.ClientSession, params: dict[str, str]) -> object:
    global _request_lock, _next_request_at, _cooldown_until
    if _request_lock is None:
        _request_lock = asyncio.Lock()
    async with _request_lock:
        if _cooldown_until > time.monotonic():
            raise ExternalServiceError("Музей временно ограничил запросы. Попробуй позже.")
        await asyncio.sleep(max(0.0, _next_request_at - time.monotonic()))
        _next_request_at = time.monotonic() + REQUEST_INTERVAL
        async with session.get(API_URL, params=params, allow_redirects=False) as response:
            if response.status == 429:
                _cooldown_until = time.monotonic() + RATE_LIMIT_COOLDOWN
                raise ExternalServiceError("Музей временно ограничил запросы. Попробуй позже.")
            if response.status != 200:
                raise ExternalServiceError("Каталог музея сейчас недоступен.")
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ExternalServiceError("Каталог музея вернул слишком большой ответ.")
            return cast(object, json.loads(body))


async def _search(session: aiohttp.ClientSession, *, limit: int, offset: int = 0) -> dict[str, object]:
    params = {"cc0": "", "has_image": "1", "type": "Painting", "limit": str(limit), "skip": str(offset), "fields": FIELDS}
    return _object(await _request_json(session, params))


async def _choose(session: aiohttp.ClientSession, recent: tuple[str, ...]) -> ArtPuzzle:
    count = await _search(session, limit=1)
    total = _object(count.get("info")).get("total")
    if type(total) is not int or total < 6:
        raise ExternalServiceError("В каталоге не нашлось достаточно картин для викторины.")
    selected: tuple[Artwork, int] | None = None
    candidates: dict[int, str] = {}
    for _ in range(MAX_ATTEMPTS):
        index = random.randrange(total)
        # Sample pages across the entire catalog, then choose a valid painting
        # from the page. This favors reliability over exact per-painting uniformity:
        # one anonymous work must not discard its already downloaded neighbors.
        # Shift the trailing window left so the last few works stay selectable.
        offset = min(index // PAGE_SIZE * PAGE_SIZE, max(0, total - PAGE_SIZE))
        page = await _search(session, limit=PAGE_SIZE, offset=offset)
        rows = _items(page.get("data"), PAGE_SIZE)
        available: list[tuple[Artwork, int]] = []
        for row in rows:
            try:
                artwork, artist_id = parse_artwork(row)
                candidates.setdefault(artist_id, artwork.artist)
                if artwork.id not in recent:
                    available.append((artwork, artist_id))
            except UnsuitableArtwork:
                continue
        # Preserve a valid question while subsequent random pages supply missing
        # distractors, rather than excluding works beside prolific artists.
        if selected is None and available:
            selected = random.choice(available)
        if selected is None:
            continue
        painting, painter = selected
        correct = painting.artist
        unique = {_label_key(correct): correct}
        for identifier, name in candidates.items():
            if identifier != painter:
                unique.setdefault(_label_key(name), name)
        if len(unique) < 6:
            continue
        alternatives = [name for key, name in unique.items() if key != _label_key(correct)]
        choices = [correct, *random.sample(alternatives, 5)]
        random.shuffle(choices)
        return ArtPuzzle(painting, tuple(choices), choices.index(correct))
    raise ExternalServiceError("Не удалось подобрать новую картину с однозначным авторством. Попробуй ещё раз.")


async def random_artwork(recent: tuple[str, ...] = ()) -> ArtPuzzle:
    """Choose one painting and six artists without storing a fixed local catalog."""
    try:
        async with asyncio.timeout(FETCH_TIMEOUT):
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT), headers={"User-Agent": USER_AGENT}
            ) as session:
                return await _choose(session, recent)
    except (aiohttp.ClientError, TimeoutError, ValueError, UnicodeError) as exc:
        raise ExternalServiceError("Не удалось загрузить картину из музея. Попробуй позже.") from exc


def _verify_image(body: bytes) -> None:
    with Image.open(io.BytesIO(body)) as image:
        if image.format != "JPEG" or image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("Unexpected image format or size")
        if image.width + image.height > 10_000 or max(image.size) > min(image.size) * 20:
            raise ValueError("Image exceeds Telegram photo limits")
        image.load()


async def download_artwork(image_url: str) -> bytes:
    """Download a bounded JPEG with the same identifiable client used by the API."""
    try:
        url = _image_url(image_url)
        async with asyncio.timeout(IMAGE_TIMEOUT):
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=IMAGE_TIMEOUT), headers={"User-Agent": USER_AGENT}
            ) as session:
                async with session.get(url, allow_redirects=False) as response:
                    if response.status != 200 or response.content_type != "image/jpeg":
                        raise ExternalServiceError("Не удалось загрузить изображение картины.")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        body.extend(chunk)
                        if len(body) > MAX_IMAGE_BYTES:
                            raise ExternalServiceError("Изображение картины слишком большое.")
                    content = bytes(body)
                    _verify_image(content)
                    return content
    except (aiohttp.ClientError, TimeoutError, ValueError, OSError, Image.DecompressionBombError) as exc:
        raise ExternalServiceError("Не удалось загрузить изображение картины. Попробуй позже.") from exc
