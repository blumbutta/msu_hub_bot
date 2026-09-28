import asyncio
import copy
import io
import json
import random

import aiohttp
import pytest
from cachetools import TTLCache
from PIL import Image

from msu_hub_bot.providers import pokemon as source
from msu_hub_bot.providers.exceptions import ExternalServiceError


def species_url(identifier):
    return f"{source.API_URL}pokemon-species/{identifier}/"


def pokemon_url(identifier):
    return f"{source.API_URL}pokemon/{identifier + 10_000}/"


def artwork_url(identifier):
    return f"{source.ARTWORK_URL}{identifier + 10_000}.png"


def species(identifier):
    return {
        "id": identifier,
        "name": f"species-{identifier}",
        "names": [{"language": {"name": "en"}, "name": f"Pokémon {identifier}"}],
        "varieties": [
            {"is_default": False, "pokemon": {"name": "other-form", "url": f"{source.API_URL}pokemon/999999/"}},
            {"is_default": True, "pokemon": {"name": f"default-{identifier}", "url": pokemon_url(identifier)}},
        ],
    }


def pokemon(identifier):
    return {
        "id": identifier + 10_000,
        "name": f"default-{identifier}",
        "is_default": True,
        "species": {"name": f"species-{identifier}", "url": species_url(identifier)},
        "sprites": {"other": {"official-artwork": {"front_default": artwork_url(identifier)}}},
    }


def catalog(identifiers):
    return {
        "count": len(identifiers),
        "next": None,
        "previous": None,
        "results": [{"name": f"species-{value}", "url": species_url(value)} for value in identifiers],
    }


def png(*, mode="RGBA", size=(30, 30), opaque=False, blank=False):
    output = io.BytesIO()
    image = Image.new(mode, size, (0, 0, 0, 255 if opaque else 0) if mode == "RGBA" else "white")
    if not blank:
        image.paste((240, 130, 10, 255) if mode == "RGBA" else "red", (5, 5, min(25, size[0]), min(25, size[1])))
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    clock = [1.0]

    def timer():
        return clock[0]

    monkeypatch.setattr(source, "_catalog_cache", TTLCache(maxsize=1, ttl=6 * 3600, timer=timer))
    monkeypatch.setattr(source, "_species_cache", TTLCache(maxsize=512, ttl=source.CACHE_TTL, timer=timer))
    monkeypatch.setattr(source, "_pokemon_cache", TTLCache(maxsize=256, ttl=source.CACHE_TTL, timer=timer))
    monkeypatch.setattr(
        source, "_image_cache", TTLCache(maxsize=source.IMAGE_CACHE_BYTES, ttl=source.CACHE_TTL, getsizeof=len, timer=timer)
    )
    monkeypatch.setattr(source, "_metadata_lock", None)
    monkeypatch.setattr(source, "_image_lock", None)
    monkeypatch.setattr(source, "_cooldown_until", 0.0)
    return clock


class Response:
    def __init__(self, value=None, *, body=None, status=200, content_type="application/json", gate=None):
        self.body = json.dumps(value).encode() if body is None else body
        self.status = status
        self.content_type = content_type
        self.content = self
        self.gate = gate
        self.entered = asyncio.Event()
        self.closed = False
        self.backend = None

    async def __aenter__(self):
        self.backend.active += 1
        self.backend.max_active = max(self.backend.max_active, self.backend.active)
        self.entered.set()
        return self

    async def __aexit__(self, *_):
        self.backend.active -= 1
        self.closed = True

    async def iter_chunked(self, size):
        if self.gate is not None:
            await self.gate.wait()
        for offset in range(0, len(self.body), size):
            await asyncio.sleep(0)
            yield self.body[offset : offset + size]


class Backend:
    def __init__(self, identifiers):
        self.catalog_url = f"{source.API_URL}pokemon-species/?limit={source.MAX_CATALOG_ENTRIES}"
        self.data = {self.catalog_url: catalog(identifiers)}
        self.data.update({species_url(value): species(value) for value in identifiers})
        self.data.update({pokemon_url(value): pokemon(value) for value in identifiers})
        self.responses = {}
        self.calls = []
        self.created = []
        self.returned = []
        self.active = 0
        self.max_active = 0
        self.closed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed += 1

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.get(url)
        if isinstance(result, Exception):
            raise result
        if result is None:
            result = Response(copy.deepcopy(self.data[url]))
        result.backend = self
        self.returned.append(result)
        return result


def install(monkeypatch, identifiers=tuple(range(1, 25))):
    backend = Backend(identifiers)

    def session(**kwargs):
        backend.created.append(kwargs)
        return backend

    monkeypatch.setattr(source.aiohttp, "ClientSession", session)
    return backend


def first_choices(monkeypatch):
    monkeypatch.setattr(source.random, "choice", lambda values: values[0])
    monkeypatch.setattr(source.random, "sample", lambda values, count: values[:count])


async def test_canonical_default_form_links_to_correct_species_and_six_english_answers(monkeypatch):
    backend = install(monkeypatch)
    first_choices(monkeypatch)
    result = await source.random_pokemon()
    assert result.pokemon == source.Pokemon("1", "Pokémon 1", artwork_url(1), species_url(1))
    assert set(result.options) == {f"Pokémon {value}" for value in range(1, 7)}
    assert result.options[result.answer] == result.pokemon.name
    assert len(backend.calls) == 8
    assert all(kwargs == {"allow_redirects": False} for _, kwargs in backend.calls)
    assert backend.max_active == 3 and backend.active == 0
    assert backend.closed == 1 and all(response.closed for response in backend.returned)
    assert backend.created[0]["headers"] == {"User-Agent": source.USER_AGENT}


async def test_full_catalog_can_choose_last_species_and_noncontiguous_large_ids(monkeypatch):
    values = (1, 4, 17, 808, 899, 1025, 123456)
    install(monkeypatch, values)
    monkeypatch.setattr(source.random, "choice", lambda values: values[-1])
    result = await source.random_pokemon()
    assert result.pokemon.id == "123456"
    assert result.pokemon.image_url == artwork_url(123456)


async def test_all_fifteen_recent_species_excluded_from_target_but_usable_as_distractors(monkeypatch):
    install(monkeypatch, tuple(range(1, 17)))
    first_choices(monkeypatch)
    result = await source.random_pokemon(tuple(str(value) for value in range(1, 16)))
    assert result.pokemon.id == "16"
    assert "Pokémon 1" in result.options


async def test_exhausted_recent_catalog_fails_without_fetching_species(monkeypatch):
    backend = install(monkeypatch, tuple(range(1, 7)))
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon(tuple(str(value) for value in range(1, 7)))
    assert len(backend.calls) == 1


async def test_successful_resources_are_reused_and_expire(monkeypatch, isolated_cache):
    backend = install(monkeypatch)
    first_choices(monkeypatch)
    await source.random_pokemon()
    await source.random_pokemon()
    assert len(backend.calls) == 8
    isolated_cache[0] += 6 * 3600
    await source.random_pokemon()
    assert len(backend.calls) == 9  # Refresh the entire catalog, preserving species metadata.
    isolated_cache[0] += source.CACHE_TTL
    await source.random_pokemon()
    assert len(backend.calls) == 17


async def test_concurrent_calls_share_cached_metadata(monkeypatch):
    backend = install(monkeypatch)
    first_choices(monkeypatch)
    first, second = await asyncio.gather(source.random_pokemon(), source.random_pokemon())
    assert first.pokemon == second.pokemon
    assert len(backend.calls) == 8


async def test_changing_catalog_reference_does_not_reuse_stale_species(monkeypatch, isolated_cache):
    backend = install(monkeypatch)
    first_choices(monkeypatch)
    await source.random_pokemon()
    isolated_cache[0] += 6 * 3600
    backend.data[backend.catalog_url]["results"][0]["name"] = "renamed"
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon()
    assert [url for url, _ in backend.calls].count(species_url(1)) == 2


@pytest.mark.parametrize("count", [True, -1, 0, 5, source.MAX_CATALOG_ENTRIES + 1, None, "24"])
async def test_invalid_catalog_count_is_rejected(monkeypatch, count):
    backend = install(monkeypatch)
    backend.data[backend.catalog_url]["count"] = count
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon()
    assert len(backend.calls) == 1 and not source._catalog_cache


@pytest.mark.parametrize(
    "field,value", [("next", "https://pokeapi.co/api/v2/pokemon-species/?offset=20"), ("previous", "x"), ("results", [])]
)
async def test_incomplete_catalog_is_never_silently_sampled(monkeypatch, field, value):
    backend = install(monkeypatch)
    backend.data[backend.catalog_url][field] = value
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon()
    assert len(backend.calls) == 1 and not source._catalog_cache


@pytest.mark.parametrize("field", ["url", "name"])
async def test_duplicate_species_in_catalog_are_rejected(monkeypatch, field):
    backend = install(monkeypatch)
    rows = backend.data[backend.catalog_url]["results"]
    rows[-1][field] = rows[0][field]
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon()
    assert len(backend.calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://pokeapi.co.evil.example/api/v2/pokemon-species/1/",
        "http://pokeapi.co/api/v2/pokemon-species/1/",
        "https://pokeapi.co/api/v2/pokemon/1/",
        "https://pokeapi.co/api/v2/pokemon-species/1/?next=evil",
        "https://pokeapi.co/api/v2/pokemon-species/1/#evil",
        "https://pokeapi.co/api/v2/pokemon-species/01/",
        "https://pokeapi.co/api/v2/pokemon-species/9223372036854775808/",
        "https://user@pokeapi.co/api/v2/pokemon-species/1/",
        "https://127.0.0.1/api/v2/pokemon-species/1/",
        None,
    ],
)
async def test_untrusted_species_url_rejected_before_any_follow_up_request(monkeypatch, url):
    backend = install(monkeypatch)
    backend.data[backend.catalog_url]["results"][0]["url"] = url
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon()
    assert len(backend.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", True),
        ("id", 999),
        ("name", "wrong"),
        ("names", []),
        ("names", None),
        ("varieties", []),
        ("varieties", [{"is_default": 1, "pokemon": {"name": "default-1", "url": pokemon_url(1)}}]),
    ],
)
async def test_incomplete_or_inconsistent_species_are_rejected(monkeypatch, field, value):
    backend = install(monkeypatch)
    first_choices(monkeypatch)
    backend.data[species_url(1)][field] = value
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon()
    assert pokemon_url(1) not in [url for url, _ in backend.calls]
    assert "1" not in source._species_cache
    assert backend.active == 0 and all(response.closed for response in backend.returned)


@pytest.mark.parametrize("field", ["names", "varieties"])
async def test_multiple_english_names_or_default_varieties_rejected(monkeypatch, field):
    backend = install(monkeypatch)
    first_choices(monkeypatch)
    row = backend.data[species_url(1)]
    row[field].append(copy.deepcopy(row[field][-1]))
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon()


@pytest.mark.parametrize("name", [None, "", "x" * 81, "a\u202eb", "a\x00b"])
async def test_missing_or_unsafe_english_display_name_rejected(monkeypatch, name):
    backend = install(monkeypatch)
    first_choices(monkeypatch)
    backend.data[species_url(1)]["names"][0]["name"] = name
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon()


async def test_english_display_spelling_is_preserved_without_guessing_translations(monkeypatch):
    backend = install(monkeypatch)
    first_choices(monkeypatch)
    backend.data[species_url(1)]["names"] = [
        {"name": "Nidoran♀", "language": {"name": "en"}},
        {"name": "ニドラン♀", "language": {"name": "ja"}},
    ]
    result = await source.random_pokemon()
    assert result.pokemon.name == "Nidoran♀"
    assert result.options[result.answer] == "Nidoran♀"


async def test_equivalent_option_names_never_produce_ambiguous_answers(monkeypatch):
    backend = install(monkeypatch)
    first_choices(monkeypatch)
    backend.data[species_url(1)]["names"][0]["name"] = "Mr. Mime"
    backend.data[species_url(2)]["names"][0]["name"] = "  Ｍｒ.   Mime "
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon()


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", 1),
        ("id", True),
        ("name", "not-default-1"),
        ("is_default", False),
        ("species", {"url": species_url(2), "name": "species-1"}),
        ("species", {"url": species_url(1), "name": "species-2"}),
        ("sprites", {"other": {"official-artwork": {"front_default": artwork_url(2)}}}),
        ("sprites", {"other": {"official-artwork": {"front_default": None}}}),
    ],
)
async def test_species_form_and_artwork_must_have_exact_matching_identities(monkeypatch, field, value):
    backend = install(monkeypatch)
    first_choices(monkeypatch)
    backend.data[pokemon_url(1)][field] = value
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon()
    assert not source._pokemon_cache


async def test_default_form_resource_cannot_point_off_provider(monkeypatch):
    backend = install(monkeypatch)
    first_choices(monkeypatch)
    backend.data[species_url(1)]["varieties"][1]["pokemon"]["url"] = "https://evil.example/form/"
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon()
    assert all(url.startswith(source.API_URL) for url, _ in backend.calls)


async def test_correct_position_varies_across_all_six_buttons(monkeypatch):
    install(monkeypatch)
    positions = set()
    for seed in range(40):
        rng = random.Random(seed)
        monkeypatch.setattr(source.random, "choice", rng.choice)
        monkeypatch.setattr(source.random, "sample", rng.sample)
        monkeypatch.setattr(source.random, "shuffle", rng.shuffle)
        result = await source.random_pokemon()
        positions.add(result.answer)
        assert len(set(result.options)) == 6 and result.options[result.answer] == result.pokemon.name
    assert positions == set(range(6))


@pytest.mark.parametrize("status", [301, 403, 404, 500])
async def test_api_http_error_or_redirect_does_not_retry(monkeypatch, status):
    backend = install(monkeypatch)
    backend.responses[backend.catalog_url] = Response(status=status)
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon()
    assert len(backend.calls) == 1 and backend.closed == 1
    assert backend.returned[0].closed


async def test_rate_limit_cooldown_prevents_repeated_api_calls(monkeypatch):
    backend = install(monkeypatch)
    backend.responses[backend.catalog_url] = Response(status=429)
    for _ in range(2):
        with pytest.raises(ExternalServiceError):
            await source.random_pokemon()
    assert len(backend.calls) == 1


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"null", b"x" * (source.MAX_RESPONSE_BYTES + 1)])
async def test_bad_or_oversized_metadata_rejected_and_not_cached(monkeypatch, body):
    backend = install(monkeypatch)
    backend.responses[backend.catalog_url] = Response(body=body)
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon()
    assert not source._catalog_cache and backend.returned[0].closed


async def test_connection_error_normalized_and_session_closed(monkeypatch):
    backend = install(monkeypatch)
    backend.responses[backend.catalog_url] = aiohttp.ClientConnectionError("offline")
    with pytest.raises(ExternalServiceError):
        await source.random_pokemon()
    assert backend.closed == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_parent_timeout_or_cancellation_closes_parallel_requests(monkeypatch, cancel):
    backend = install(monkeypatch)
    first_choices(monkeypatch)
    response = Response(species(1), gate=asyncio.Event())
    backend.responses[species_url(1)] = response
    monkeypatch.setattr(source, "FETCH_TIMEOUT", 14 if cancel else 0.02)
    task = asyncio.create_task(source.random_pokemon())
    await response.entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ExternalServiceError):
        await task
    assert backend.closed == 1 and backend.active == 0
    assert all(item.closed for item in backend.returned)
    assert not source._metadata_lock.locked()


async def test_waiting_for_other_chat_counts_in_whole_operation_timeout(monkeypatch):
    backend = install(monkeypatch)
    lock = asyncio.Lock()
    await lock.acquire()
    monkeypatch.setattr(source, "_metadata_lock", lock)
    monkeypatch.setattr(source, "FETCH_TIMEOUT", 0.01)
    try:
        with pytest.raises(ExternalServiceError):
            await source.random_pokemon()
        assert not backend.created and not backend.calls
    finally:
        lock.release()


async def test_image_is_verified_cached_in_memory_and_expires(monkeypatch, isolated_cache):
    backend = install(monkeypatch)
    body = png()
    backend.responses[artwork_url(1)] = Response(body=body, content_type="image/png")
    assert await source.download_pokemon(artwork_url(1)) == body
    assert await source.download_pokemon(artwork_url(1)) == body
    assert backend.calls == [(artwork_url(1), {"allow_redirects": False})]
    assert backend.closed == 1 and backend.returned[0].closed
    isolated_cache[0] += source.CACHE_TTL
    assert await source.download_pokemon(artwork_url(1)) == body
    assert len(backend.calls) == 2


async def test_image_cache_eviction_is_bounded_by_bytes(monkeypatch):
    backend = install(monkeypatch)
    body = png()
    monkeypatch.setattr(source, "_image_cache", TTLCache(maxsize=len(body), ttl=60, getsizeof=len))
    for value in [1, 2, 1]:
        backend.responses[artwork_url(value)] = Response(body=body, content_type="image/png")
        await source.download_pokemon(artwork_url(value))
        assert source._image_cache.currsize <= len(body)
    assert len(backend.calls) == 3


@pytest.mark.parametrize(
    "url",
    [
        None,
        "http://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/1.png",
        "https://raw.githubusercontent.com.evil.example/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/1.png",
        "https://raw.githubusercontent.com/Other/sprites/master/sprites/pokemon/other/official-artwork/1.png",
        "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/../1.png",
        "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/%31.png",
        "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/1.png?x=1",
        "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/1.png#x",
        "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/1.png",
        "https://user@raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/1.png",
        "https://127.0.0.1/private.png",
    ],
)
async def test_untrusted_image_urls_fail_before_network(monkeypatch, url):
    backend = install(monkeypatch)
    with pytest.raises(ExternalServiceError):
        await source.download_pokemon(url)
    assert not backend.calls and not backend.created


@pytest.mark.parametrize(
    "body,content_type,status",
    [
        (b"redirect", "image/png", 301),
        (b"error", "image/png", 404),
        (b"<html>error</html>", "text/html", 200),
        (b"not png", "image/png", 200),
        (png()[:-16], "image/png", 200),
        (png(mode="RGB"), "image/png", 200),
        (png(opaque=True), "image/png", 200),
        (png(blank=True), "image/png", 200),
        (b"x" * (source.MAX_IMAGE_BYTES + 1), "image/png", 200),
        (png(size=(1001, 1000)), "image/png", 200),
    ],
)
async def test_unusable_silhouette_or_oversized_invalid_image_rejected(monkeypatch, body, content_type, status):
    backend = install(monkeypatch)
    backend.responses[artwork_url(1)] = Response(body=body, content_type=content_type, status=status)
    with pytest.raises(ExternalServiceError):
        await source.download_pokemon(artwork_url(1))
    assert not source._image_cache and backend.returned[0].closed and backend.closed == 1


async def test_palette_png_transparency_is_supported(monkeypatch):
    backend = install(monkeypatch)
    output = io.BytesIO()
    image = Image.new("P", (30, 30))
    image.paste(1, (5, 5, 25, 25))
    image.save(output, format="PNG", transparency=0)
    body = output.getvalue()
    backend.responses[artwork_url(1)] = Response(body=body, content_type="image/png")
    assert await source.download_pokemon(artwork_url(1)) == body


@pytest.mark.parametrize("cancel", [False, True])
async def test_image_timeout_and_cancellation_release_resources(monkeypatch, cancel):
    backend = install(monkeypatch)
    response = Response(body=png(), content_type="image/png", gate=asyncio.Event())
    backend.responses[artwork_url(1)] = response
    monkeypatch.setattr(source, "IMAGE_TIMEOUT", 8 if cancel else 0.02)
    task = asyncio.create_task(source.download_pokemon(artwork_url(1)))
    await response.entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ExternalServiceError):
        await task
    assert backend.closed == 1 and response.closed and not source._image_lock.locked()
    assert not source._image_cache
