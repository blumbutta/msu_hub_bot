"""Bounded, cached Pokémon questions and transparent official artwork from PokéAPI."""

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
from cachetools import TTLCache
from PIL import Image

from msu_hub_bot.providers.exceptions import ExternalServiceError

API_URL = "https://pokeapi.co/api/v2/"
ARTWORK_URL = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/"
USER_AGENT = "MSUHubBot-Pokemon/1.0 (https://github.com/uburuntu/msu_hub_bot)"
FETCH_TIMEOUT = 14.0
IMAGE_TIMEOUT = 8.0
MAX_RESPONSE_BYTES = 2_000_000
MAX_IMAGE_BYTES = 2_000_000
MAX_IMAGE_PIXELS = 1_000_000
# A resource bound, not a list of supported generations or a maximum species ID.
# A catalog exceeding this bound is rejected rather than silently truncated.
MAX_CATALOG_ENTRIES = 10_000
PARALLEL_REQUESTS = 3
CACHE_TTL = 24 * 60 * 60
IMAGE_CACHE_BYTES = 16_000_000
_metadata_lock: asyncio.Lock | None = None
_image_lock: asyncio.Lock | None = None
_cooldown_until = 0.0


@dataclass(frozen=True)
class Pokemon:
    id: str
    name: str
    image_url: str
    source_url: str


@dataclass(frozen=True)
class PokemonPuzzle:
    pokemon: Pokemon
    options: tuple[str, ...]
    answer: int


@dataclass(frozen=True)
class _SpeciesRef:
    id: str
    slug: str
    url: str


@dataclass(frozen=True)
class _Species:
    ref: _SpeciesRef
    name: str
    pokemon_id: str
    pokemon_slug: str
    pokemon_url: str


# PokéAPI asks clients to cache resources. Keep only validated, bounded values
# in RAM; sessions and user/game state never live in these caches.
_catalog_cache: TTLCache[str, tuple[_SpeciesRef, ...]] = TTLCache(maxsize=1, ttl=6 * 60 * 60)
_species_cache: TTLCache[str, _Species] = TTLCache(maxsize=512, ttl=CACHE_TTL)
_pokemon_cache: TTLCache[str, Pokemon] = TTLCache(maxsize=256, ttl=CACHE_TTL)
_image_cache: TTLCache[str, bytes] = TTLCache(maxsize=IMAGE_CACHE_BYTES, ttl=CACHE_TTL, getsizeof=len)


class UnsuitablePokemon(ValueError):
    """The response cannot establish a unique species, name or default artwork."""


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise UnsuitablePokemon("Expected an object")
    return cast(dict[str, object], value)


def _items(value: object, maximum: int) -> list[object]:
    if not isinstance(value, list) or len(value) > maximum:
        raise UnsuitablePokemon("Invalid result list")
    return cast(list[object], value)


def _identifier(value: object) -> str:
    if type(value) is not int or not 0 < value < 2**63:
        raise UnsuitablePokemon("Invalid identifier")
    return str(value)


def _slug(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) or len(value) > 80:
        raise UnsuitablePokemon("Invalid resource name")
    return value


def _name(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise UnsuitablePokemon("Missing or oversized display name")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise UnsuitablePokemon("Unsupported display name controls")
    return " ".join(value.split())


def _name_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _resource_url(value: object, resource: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise UnsuitablePokemon("Missing resource URL")
    match = re.fullmatch(re.escape(API_URL + resource + "/") + r"([1-9][0-9]{0,18})/", value)
    if match is None or int(match[1]) >= 2**63:
        raise UnsuitablePokemon("Untrusted resource URL")
    return value, match[1]


def _image_url(value: object) -> str:
    if not isinstance(value, str):
        raise UnsuitablePokemon("Missing official artwork")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com" or parsed.query or parsed.fragment:
        raise UnsuitablePokemon("Untrusted image origin")
    if not re.fullmatch(re.escape(ARTWORK_URL) + r"[1-9][0-9]{0,18}\.png", value):
        raise UnsuitablePokemon("Invalid official artwork path")
    return value


def _catalog(value: object) -> tuple[_SpeciesRef, ...]:
    record = _object(value)
    count = record.get("count")
    if type(count) is not int or not 6 <= count <= MAX_CATALOG_ENTRIES:
        raise UnsuitablePokemon("Invalid species count")
    rows = _items(record.get("results"), MAX_CATALOG_ENTRIES)
    if len(rows) != count or record.get("next", True) is not None or record.get("previous", True) is not None:
        raise UnsuitablePokemon("Incomplete species catalog")
    species = []
    for row in rows:
        item = _object(row)
        url, identifier = _resource_url(item.get("url"), "pokemon-species")
        species.append(_SpeciesRef(identifier, _slug(item.get("name")), url))
    if len({item.id for item in species}) != count or len({item.slug for item in species}) != count:
        raise UnsuitablePokemon("Duplicate species in catalog")
    return tuple(species)


def _parse_species(value: object, ref: _SpeciesRef) -> _Species:
    record = _object(value)
    if _identifier(record.get("id")) != ref.id or record.get("name") != ref.slug:
        raise UnsuitablePokemon("Species identity mismatch")
    names = [_object(item) for item in _items(record.get("names"), 100)]
    english = [item for item in names if _object(item.get("language")).get("name") == "en"]
    if len(english) != 1:
        raise UnsuitablePokemon("Missing or ambiguous English species name")
    varieties = [_object(item) for item in _items(record.get("varieties"), 100)]
    defaults = [item for item in varieties if item.get("is_default") is True]
    if len(defaults) != 1:
        raise UnsuitablePokemon("Missing or ambiguous default variety")
    variety = _object(defaults[0].get("pokemon"))
    url, pokemon_id = _resource_url(variety.get("url"), "pokemon")
    return _Species(ref, _name(english[0].get("name")), pokemon_id, _slug(variety.get("name")), url)


def _parse_pokemon(value: object, species: _Species) -> Pokemon:
    record = _object(value)
    if (
        _identifier(record.get("id")) != species.pokemon_id
        or record.get("name") != species.pokemon_slug
        or record.get("is_default") is not True
    ):
        raise UnsuitablePokemon("Default variety identity mismatch")
    reference = _object(record.get("species"))
    if reference.get("url") != species.ref.url or reference.get("name") != species.ref.slug:
        raise UnsuitablePokemon("Default variety belongs to another species")
    sprites = _object(_object(_object(record.get("sprites")).get("other")).get("official-artwork"))
    image_url = _image_url(sprites.get("front_default"))
    if image_url != f"{ARTWORK_URL}{species.pokemon_id}.png":
        raise UnsuitablePokemon("Artwork identity mismatch")
    return Pokemon(species.ref.id, species.name, image_url, species.ref.url)


async def _read(response: aiohttp.ClientResponse, maximum: int) -> bytes:
    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > maximum:
            raise UnsuitablePokemon("Provider response is too large")
    return bytes(body)


async def _request_json(session: aiohttp.ClientSession, url: str) -> object:
    global _cooldown_until
    if _cooldown_until > time.monotonic():
        raise ExternalServiceError("PokéAPI временно ограничил запросы. Попробуй позже.")
    async with session.get(url, allow_redirects=False) as response:
        if response.status == 429:
            _cooldown_until = time.monotonic() + 60
            raise ExternalServiceError("PokéAPI временно ограничил запросы. Попробуй позже.")
        if response.status != 200 or response.content_type != "application/json":
            raise ExternalServiceError("Каталог покемонов сейчас недоступен.")
        return cast(object, json.loads(await _read(response, MAX_RESPONSE_BYTES)))


async def _load_species(session: aiohttp.ClientSession, ref: _SpeciesRef, semaphore: asyncio.Semaphore) -> _Species:
    cached = _species_cache.get(ref.id)
    if cached is not None and cached.ref == ref:
        return cached
    async with semaphore:
        result = _parse_species(await _request_json(session, ref.url), ref)
    _species_cache[ref.id] = result
    return result


async def _choose(session: aiohttp.ClientSession, exclude: tuple[str, ...]) -> PokemonPuzzle:
    catalog = _catalog_cache.get("species")
    if catalog is None:
        catalog = _catalog(await _request_json(session, f"{API_URL}pokemon-species/?limit={MAX_CATALOG_ENTRIES}"))
        _catalog_cache["species"] = catalog
    available = [item for item in catalog if item.id not in exclude]
    if not available:
        raise UnsuitablePokemon("No unseen species remain")
    target = random.choice(available)
    choices = [target, *random.sample([item for item in catalog if item.id != target.id], 5)]
    semaphore = asyncio.Semaphore(PARALLEL_REQUESTS)
    # return_exceptions waits for every child before closing its shared session.
    # Parent cancellation still cancels and awaits all outstanding children.
    results = await asyncio.gather(*(_load_species(session, item, semaphore) for item in choices), return_exceptions=True)
    species = []
    for result in results:
        if isinstance(result, BaseException):
            raise result
        species.append(result)
    names = [item.name for item in species]
    if len({_name_key(name) for name in names}) != 6:
        raise UnsuitablePokemon("Ambiguous display names in options")
    selected = species[0]
    pokemon = _pokemon_cache.get(selected.ref.id)
    if pokemon is None or pokemon.name != selected.name or pokemon.image_url != f"{ARTWORK_URL}{selected.pokemon_id}.png":
        pokemon = _parse_pokemon(await _request_json(session, selected.pokemon_url), selected)
        _pokemon_cache[selected.ref.id] = pokemon
    random.shuffle(names)
    return PokemonPuzzle(pokemon, tuple(names), names.index(pokemon.name))


async def random_pokemon(exclude: tuple[str, ...] = ()) -> PokemonPuzzle:
    """Choose uniformly from the live species catalog, excluding recent targets."""
    global _metadata_lock
    if _metadata_lock is None:
        _metadata_lock = asyncio.Lock()
    try:
        async with asyncio.timeout(FETCH_TIMEOUT), _metadata_lock:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT), headers={"User-Agent": USER_AGENT}
            ) as session:
                return await _choose(session, exclude)
    except (aiohttp.ClientError, TimeoutError, ValueError, UnicodeError) as exc:
        raise ExternalServiceError("Не удалось загрузить покемона. Попробуй позже.") from exc


def _verify_image(body: bytes) -> None:
    if not body.startswith(b"\x89PNG\r\n\x1a\n") or not body.endswith(b"\0\0\0\0IEND\xaeB`\x82"):
        raise UnsuitablePokemon("Incomplete PNG stream")
    with Image.open(io.BytesIO(body)) as image:
        if image.format != "PNG" or image.width * image.height > MAX_IMAGE_PIXELS or getattr(image, "is_animated", False):
            raise UnsuitablePokemon("Unexpected image format or dimensions")
        image.verify()
    with Image.open(io.BytesIO(body)) as image:
        image.load()
        # Palette PNGs may carry transparency in metadata rather than an A band.
        if "A" not in image.getbands() and "transparency" not in image.info:
            raise UnsuitablePokemon("Artwork lacks transparency")
        alpha = image.convert("RGBA").getchannel("A")
        if alpha.getextrema() != (0, 255):
            raise UnsuitablePokemon("Artwork has no usable silhouette")


async def download_pokemon(image_url: str) -> bytes:
    """Return a complete transparent PNG, cached in bounded RAM and never on disk."""
    global _image_lock
    if _image_lock is None:
        _image_lock = asyncio.Lock()
    try:
        url = _image_url(image_url)
        async with asyncio.timeout(IMAGE_TIMEOUT), _image_lock:
            cached = _image_cache.get(url)
            if cached is not None:
                return cached
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=IMAGE_TIMEOUT), headers={"User-Agent": USER_AGENT}
            ) as session:
                async with session.get(url, allow_redirects=False) as response:
                    if response.status != 200 or response.content_type != "image/png":
                        raise ExternalServiceError("Не удалось загрузить картинку покемона.")
                    body = await _read(response, MAX_IMAGE_BYTES)
                    _verify_image(body)
                    _image_cache[url] = body
                    return body
    except (aiohttp.ClientError, TimeoutError, ValueError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ExternalServiceError("Не удалось загрузить картинку покемона. Попробуй позже.") from exc
