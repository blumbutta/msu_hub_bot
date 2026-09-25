import asyncio
import io
import json
import random

import aiohttp
import pytest
from PIL import Image

from msu_hub_bot.providers import art as source
from msu_hub_bot.providers.exceptions import ExternalServiceError


def painting(identifier=100, artist_id=None, **overrides):
    artist_id = identifier + 1000 if artist_id is None else artist_id
    return {
        "id": identifier,
        "accession_number": f"1900.{identifier}",
        "title": f"Painting {identifier}",
        "creators": [
            {
                "id": artist_id,
                "description": f"Painter {artist_id} (Dutch, 1800–1870)",
                "birth_year": "1800",
                "death_year": "1870",
                "role": "artist",
                "extent": None,
                "qualifier": None,
            }
        ],
        "creation_date": "1860",
        "images": {"web": {"url": f"https://{source.IMAGE_HOST}/1900.{identifier}/1900.{identifier}_web.jpg"}},
        "share_license_status": "CC0",
        "type": "Painting",
        **overrides,
    }


@pytest.fixture(autouse=True)
def isolated_requests(monkeypatch):
    monkeypatch.setattr(source, "_request_lock", None)
    monkeypatch.setattr(source, "_next_request_at", 0.0)
    monkeypatch.setattr(source, "_cooldown_until", 0.0)
    monkeypatch.setattr(source, "REQUEST_INTERVAL", 0.0)


class Response:
    def __init__(self, value=None, *, status=200, body=None, gate=None, content_type="application/json"):
        self.status = status
        self.body = json.dumps(value).encode() if body is None else body
        self.content = self
        self.content_type = content_type
        self.gate = gate
        self.entered = asyncio.Event()
        self.closed = False

    async def __aenter__(self):
        self.entered.set()
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def iter_chunked(self, size):
        if self.gate is not None:
            await self.gate.wait()
        for offset in range(0, len(self.body), size):
            await asyncio.sleep(0)
            yield self.body[offset : offset + size]


class Museum:
    def __init__(self, paintings, responses=None):
        self.paintings = paintings
        self.responses = responses
        self.calls = []
        self.returned = []
        self.closed = False
        self.created = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.responses is not None:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
        else:
            assert url == source.API_URL
            query = kwargs["params"]
            offset = int(query["skip"])
            result = Response({"info": {"total": len(self.paintings)}, "data": self.paintings[offset : offset + int(query["limit"])]})
        self.returned.append(result)
        return result


def install(monkeypatch, paintings=None, responses=None):
    museum = Museum(paintings if paintings is not None else [painting(i) for i in range(100, 180)], responses)

    def session(**kwargs):
        museum.created.append(kwargs)
        return museum

    monkeypatch.setattr(source.aiohttp, "ClientSession", session)
    return museum


def test_painting_uses_trusted_image_and_source_without_translating_names():
    row = painting(100, creation_date=None)
    row["creators"][0]["description"] = "Édouard Manet (French, 1832–1883)"
    art, author = source.parse_artwork(row)
    assert author == 1100
    assert art.artist == "Édouard Manet" and art.date == ""
    assert art.image_url == f"https://{source.IMAGE_HOST}/1900.100/1900.100_web.jpg"
    assert art.source_url == "https://www.clevelandart.org/art/1900.100"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", True),
        ("id", -1),
        ("id", "https://other.example"),
        ("creators", []),
        ("creators", "artist"),
        ("creators", None),
        ("share_license_status", "Copyrighted"),
        ("share_license_status", True),
        ("type", "Print"),
        ("images", None),
        ("images", {"web": None}),
        ("title", ""),
        ("title", None),
        ("title", "x" * 501),
        ("title", "x\u202ey"),
        ("creation_date", []),
        ("accession_number", "../private"),
        ("accession_number", None),
    ],
)
def test_incomplete_unsafe_or_ambiguous_records_are_rejected(field, value):
    with pytest.raises(source.UnsuitableArtwork):
        source.parse_artwork(painting(**{field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", True),
        ("id", None),
        ("qualifier", "attributed to"),
        ("extent", "landscape only"),
        ("role", "publisher"),
        ("role", ""),
        ("description", "Unknown"),
        ("description", None),
        ("description", "Attributed to Painter Name"),
        ("description", "Painter " + "x" * 600),
    ],
)
def test_creator_must_have_unqualified_personal_authorship(field, value):
    row = painting()
    row["creators"][0][field] = value
    with pytest.raises(source.UnsuitableArtwork):
        source.parse_artwork(row)


def test_coauthored_picture_cannot_become_a_single_answer_question():
    row = painting()
    row["creators"] += painting(101)["creators"]
    with pytest.raises(source.UnsuitableArtwork):
        source.parse_artwork(row)


@pytest.mark.parametrize(
    "qualifier", ["Workshop of", "School of", "Studio of", "Circle of", "Follower of", "After", "Probably", "Copy after"]
)
def test_uncertain_attribution_cannot_become_the_correct_answer(qualifier):
    row = painting()
    row["creators"][0]["description"] = f"{qualifier} Painter Name (Dutch, 1800–1870)"
    with pytest.raises(source.UnsuitableArtwork):
        source.parse_artwork(row)


@pytest.mark.parametrize("birth,death", [(None, None), ("0", "0"), (True, False), ("", "")])
def test_collectives_and_unknown_creators_need_real_biographical_dates(birth, death):
    row = painting()
    row["creators"][0].update(description="Persian", birth_year=birth, death_year=death)
    with pytest.raises(source.UnsuitableArtwork):
        source.parse_artwork(row)


def test_uncertain_death_date_does_not_invalidate_named_painter_with_known_birth():
    row = painting()
    row["creators"][0].update(description="Victor Dubreuil (American, 1842–after 1910)", birth_year="1842", death_year="1910")
    art, _ = source.parse_artwork(row)
    assert art.artist == "Victor Dubreuil"


@pytest.mark.parametrize(
    "url",
    [
        None,
        "http://openaccess-cdn.clevelandart.org/1900.100/1900.100_web.jpg",
        "https://openaccess-cdn.clevelandart.org.evil.example/1900.100/1900.100_web.jpg",
        "https://127.0.0.1/private.jpg",
        "https://openaccess-cdn.clevelandart.org/../a_web.jpg",
        "https://openaccess-cdn.clevelandart.org/%2e%2e/a_web.jpg",
        "https://openaccess-cdn.clevelandart.org/1900.100/1900.100_full.tif",
        "https://openaccess-cdn.clevelandart.org/1900.100/1900.100_print.jpg",
        "https://openaccess-cdn.clevelandart.org/1900.100/1900.100_web.jpg?redirect=evil",
        "https://user:pass@openaccess-cdn.clevelandart.org/1900.100/1900.100_web.jpg",
    ],
)
def test_metadata_cannot_redirect_image_fetch_to_another_origin_or_asset(url):
    with pytest.raises(source.UnsuitableArtwork):
        source.parse_artwork(painting(images={"web": {"url": url}}))


async def test_six_distinct_artist_answers_include_correct_and_cleanup(monkeypatch):
    museum = install(monkeypatch)
    result = await source.random_artwork()
    assert len(result.options) == len(set(result.options)) == 6
    assert result.options[result.answer] == result.artwork.artist
    assert result.options.count(result.artwork.artist) == 1
    assert museum.closed and all(response.closed for response in museum.returned)
    assert len(museum.calls) == 2
    for url, kwargs in museum.calls:
        assert url == source.API_URL and kwargs["allow_redirects"] is False
        assert kwargs["params"]["cc0"] == "" and kwargs["params"]["has_image"] == "1" and kwargs["params"]["type"] == "Painting"
    assert museum.created[0]["headers"] == {"User-Agent": source.USER_AGENT}


@pytest.mark.parametrize("index", [0, 39, 40, 79])
async def test_random_selection_reaches_both_ends_of_full_collection(monkeypatch, index):
    install(monkeypatch)
    monkeypatch.setattr(source.random, "randrange", lambda total: index if total == 80 else pytest.fail("Catalog truncated"))
    result = await source.random_artwork()
    page_start = 100 + index // 40 * 40
    assert page_start <= int(result.artwork.id) < page_start + 40


@pytest.mark.parametrize("total", [41, 45, 46])
async def test_last_partial_page_does_not_make_its_paintings_unselectable(monkeypatch, total):
    install(monkeypatch, [painting(i) for i in range(1, total + 1)])
    monkeypatch.setattr(source.random, "randrange", lambda size: size - 1)
    monkeypatch.setattr(source.random, "choice", lambda items: items[-1])
    result = await source.random_artwork()
    assert result.artwork.id == str(total)


async def test_selected_painting_is_kept_while_other_pages_supply_distractors(monkeypatch):
    rows = [painting(i, artist_id=1000) for i in range(1, 41)] + [painting(i) for i in range(41, 81)]
    museum = install(monkeypatch, rows)
    indices = iter([4, 61])
    monkeypatch.setattr(source.random, "randrange", lambda total: next(indices))
    monkeypatch.setattr(source.random, "choice", lambda items: items[0])
    result = await source.random_artwork()
    assert result.artwork.id == "1" and result.artwork.artist == "Painter 1000"
    assert len(result.options) == 6 and len(museum.calls) == 3


async def test_recent_paintings_are_never_selected_even_when_provider_returns_them(monkeypatch):
    install(monkeypatch)
    indices = iter([0, 14, 15])
    monkeypatch.setattr(source.random, "randrange", lambda total: next(indices))
    recent = tuple(str(value) for value in range(100, 115))
    result = await source.random_artwork(recent)
    assert result.artwork.id not in recent
    assert 115 <= int(result.artwork.id) < 140


async def test_one_ambiguous_record_does_not_discard_valid_neighbors(monkeypatch):
    rows = [painting(i) for i in range(1, 81)]
    rows[1]["creators"][0]["qualifier"] = "Attributed to"
    museum = install(monkeypatch, rows)
    indices = iter([1, 61])
    monkeypatch.setattr(source.random, "randrange", lambda total: next(indices))
    result = await source.random_artwork()
    assert 1 <= int(result.artwork.id) <= 40 and result.artwork.id != "2"
    assert len(museum.calls) == 2


async def test_options_exclude_multiple_works_by_same_author_and_equivalent_names(monkeypatch):
    rows = [painting(i) for i in range(1, 9)]
    names = ["Canonical Author", " CANONICAL  author ", "Second Author", "Ｓｅｃｏｎｄ Author", "Third", "Fourth", "Fifth", "Sixth"]
    for row, name in zip(rows, names):
        row["creators"][0]["description"] = name
    install(monkeypatch, rows)
    monkeypatch.setattr(source.random, "randrange", lambda total: 0)
    monkeypatch.setattr(source.random, "choice", lambda items: items[0])
    result = await source.random_artwork()
    assert result.artwork.artist == "Canonical Author"
    assert set(result.options) == {"Canonical Author", "Second Author", "Third", "Fourth", "Fifth", "Sixth"}


async def test_correct_option_position_varies(monkeypatch):
    positions = set()
    for seed in range(30):
        install(monkeypatch)
        rng = random.Random(seed)
        monkeypatch.setattr(source.random, "randrange", rng.randrange)
        monkeypatch.setattr(source.random, "choice", rng.choice)
        monkeypatch.setattr(source.random, "sample", rng.sample)
        monkeypatch.setattr(source.random, "shuffle", rng.shuffle)
        result = await source.random_artwork()
        positions.add(result.answer)
        assert result.options.count(result.artwork.artist) == 1
    assert positions == set(range(6))


async def test_insufficient_distinct_painters_has_finite_attempt_budget(monkeypatch):
    museum = install(monkeypatch, [painting(i, artist_id=1000) for i in range(1, 41)])
    with pytest.raises(ExternalServiceError):
        await source.random_artwork()
    assert len(museum.calls) == source.MAX_ATTEMPTS + 1 and museum.closed


@pytest.mark.parametrize("total", [0, 5, True, None])
async def test_empty_or_invalid_collection_fails_promptly(monkeypatch, total):
    museum = install(monkeypatch, responses=[Response({"info": {"total": total}})])
    with pytest.raises(ExternalServiceError):
        await source.random_artwork()
    assert len(museum.calls) == 1


@pytest.mark.parametrize("status", [301, 403, 500])
async def test_http_errors_fail_promptly_and_never_follow_redirects(monkeypatch, status):
    response = Response(status=status)
    museum = install(monkeypatch, responses=[response])
    with pytest.raises(ExternalServiceError):
        await source.random_artwork()
    assert len(museum.calls) == 1 and response.closed and museum.closed


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"null", b"x" * (source.MAX_RESPONSE_BYTES + 1)])
async def test_malformed_and_oversized_responses_are_bounded(monkeypatch, body):
    response = Response(body=body)
    museum = install(monkeypatch, responses=[response])
    with pytest.raises(ExternalServiceError):
        await source.random_artwork()
    assert response.closed and museum.closed


async def test_connection_error_is_normalized(monkeypatch):
    museum = install(monkeypatch, responses=[aiohttp.ClientConnectionError("offline")])
    with pytest.raises(ExternalServiceError):
        await source.random_artwork()
    assert museum.closed


async def test_rate_limit_sets_process_wide_cooldown(monkeypatch):
    museum = install(monkeypatch, responses=[Response(status=429)])
    before = source.time.monotonic()
    with pytest.raises(ExternalServiceError):
        await source.random_artwork()
    assert source._cooldown_until >= before + 60
    with pytest.raises(ExternalServiceError):
        await asyncio.wait_for(source.random_artwork(), timeout=0.1)
    assert len(museum.calls) == 1


async def test_pacing_applies_across_independent_calls(monkeypatch):
    museum = install(monkeypatch)
    monkeypatch.setattr(source, "REQUEST_INTERVAL", 0.02)
    before = source.time.monotonic()
    await source.random_artwork()
    await source.random_artwork()
    assert len(museum.calls) == 4
    assert source.time.monotonic() - before >= 0.06


async def test_waiting_for_other_chat_counts_towards_whole_timeout(monkeypatch):
    lock = asyncio.Lock()
    await lock.acquire()
    monkeypatch.setattr(source, "_request_lock", lock)
    monkeypatch.setattr(source, "FETCH_TIMEOUT", 0.02)
    museum = install(monkeypatch)
    try:
        with pytest.raises(ExternalServiceError):
            await source.random_artwork()
        assert not museum.calls and museum.closed
    finally:
        lock.release()


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_release_response_session_and_lock(monkeypatch, cancel):
    response = Response(gate=asyncio.Event())
    museum = install(monkeypatch, responses=[response])
    monkeypatch.setattr(source, "FETCH_TIMEOUT", 16 if cancel else 0.02)
    task = asyncio.create_task(source.random_artwork())
    await response.entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ExternalServiceError):
        await task
    assert museum.closed and response.closed and not source._request_lock.locked()


def jpeg(size=(10, 10), image_format="JPEG"):
    output = io.BytesIO()
    Image.new("RGB", size).save(output, format=image_format)
    return output.getvalue()


async def test_image_download_uses_identified_client_and_returns_verified_jpeg(monkeypatch):
    body = jpeg()
    response = Response(body=body, content_type="image/jpeg")
    museum = install(monkeypatch, responses=[response])
    url = painting()["images"]["web"]["url"]
    assert await source.download_artwork(url) == body
    assert museum.calls == [(url, {"allow_redirects": False})]
    assert museum.created[0]["headers"] == {"User-Agent": source.USER_AGENT}
    assert museum.closed and response.closed


@pytest.mark.parametrize(
    "response",
    [
        Response(status=301),
        Response(status=403),
        Response(body=b"<html>challenge</html>", content_type="text/html"),
        Response(body=b"not jpeg", content_type="image/jpeg"),
        Response(body=jpeg()[:-16], content_type="image/jpeg"),
        Response(body=jpeg(image_format="PNG"), content_type="image/jpeg"),
        Response(body=b"x" * (source.MAX_IMAGE_BYTES + 1), content_type="image/jpeg"),
        Response(body=jpeg(size=(1, 40)), content_type="image/jpeg"),
    ],
)
async def test_image_download_rejects_redirects_errors_wrong_formats_and_limits(monkeypatch, response):
    museum = install(monkeypatch, responses=[response])
    with pytest.raises(ExternalServiceError):
        await source.download_artwork(painting()["images"]["web"]["url"])
    assert museum.closed and response.closed


async def test_image_untrusted_url_is_rejected_before_network(monkeypatch):
    museum = install(monkeypatch, responses=[])
    with pytest.raises(ExternalServiceError):
        await source.download_artwork("https://example.com/a_web.jpg")
    assert not museum.calls and not museum.created


@pytest.mark.parametrize("cancel", [False, True])
async def test_image_download_deadline_and_cancellation_close_resources(monkeypatch, cancel):
    response = Response(body=jpeg(), content_type="image/jpeg", gate=asyncio.Event())
    museum = install(monkeypatch, responses=[response])
    monkeypatch.setattr(source, "IMAGE_TIMEOUT", 8 if cancel else 0.02)
    task = asyncio.create_task(source.download_artwork(painting()["images"]["web"]["url"]))
    await response.entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ExternalServiceError):
        await task
    assert museum.closed and response.closed


async def test_wholly_unsuitable_page_retries_without_fixed_fallback(monkeypatch):
    rows = [painting(i, creators=[]) for i in range(1, 41)] + [painting(i) for i in range(41, 81)]
    museum = install(monkeypatch, rows)
    indices = iter([0, 60])
    monkeypatch.setattr(source.random, "randrange", lambda total: next(indices))
    result = await source.random_artwork()
    assert 41 <= int(result.artwork.id) <= 80 and len(museum.calls) == 3


async def test_question_can_vary_within_same_random_page(monkeypatch):
    selected = set()
    for seed in range(20):
        install(monkeypatch)
        rng = random.Random(seed)
        monkeypatch.setattr(source.random, "randrange", lambda total: 0)
        monkeypatch.setattr(source.random, "choice", rng.choice)
        selected.add((await source.random_artwork()).artwork.id)
    assert len(selected) > 10
