import asyncio
import importlib.util
import io
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest
from PIL import Image

from common.externals.exceptions import ExternalServiceError

spec = importlib.util.spec_from_file_location(
    "animegan", Path(__file__).resolve().parents[1] / "common/externals/animegan.py"
)
animegan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(animegan)


class Content:
    def __init__(self, data):
        self.data = data

    def __aiter__(self):
        return self.lines()

    async def lines(self):
        for line in self.data.splitlines(keepends=True):
            yield line

    async def iter_chunked(self, size):
        for index in range(0, len(self.data), size):
            yield self.data[index : index + size]


class Response:
    def __init__(self, data=None, body=b"", status=200):
        self.data, self.status, self.content = data, status, Content(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.data


class Session(Response):
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)

    post = get = request


def run(responses):
    session = Session(responses)
    with patch.object(animegan.aiohttp, "ClientSession", return_value=session):
        result = asyncio.run(animegan.mask_anime2(io.BytesIO(b"input")))
    return result, session


def setup_responses(body=None):
    if body is None:
        body = b'event: heartbeat\ndata: null\n\nevent: complete\ndata: [{"path":"/tmp/result.png"}]\n\n'
    return [
        Response(["/tmp/input.png"]),
        Response({"event_id": "abc123"}),
        Response(body=body),
    ]


def test_success():
    image = io.BytesIO()
    Image.new("RGB", (16, 16)).save(image, format="PNG")
    result, session = run(setup_responses() + [Response(body=image.getvalue())])
    assert result.tell() == 0
    assert result.getvalue() == image.getvalue()
    assert session.calls[1][1]["json"]["data"][1] == "Version 2"
    assert session.calls[3][0] == animegan.API + "/file=/tmp/result.png"
    assert session.calls[3][1]["allow_redirects"] is False
    assert len(session.calls) == 4


@pytest.mark.parametrize("status", [404, 429, 503])
def test_http_failure(status):
    with pytest.raises(ExternalServiceError, match="AnimeGAN"):
        run([Response(status=status)])


@pytest.mark.parametrize(
    "body",
    [
        b"event: error\ndata: null\n\n",
        b"event: heartbeat\ndata: null\n\n",
        b"event: complete\ndata: broken\n\n",
        b"event: complete\ndata: []\n\n",
        b'event: complete\ndata: [{"path":"https://evil.invalid/file"}]\n\n',
    ],
)
def test_queue_failure(body):
    with pytest.raises(ExternalServiceError):
        run(setup_responses(body))


def test_invalid_image():
    with pytest.raises(ExternalServiceError, match="некорректный"):
        run(setup_responses() + [Response(body=b"not an image")])


def test_oversized_result():
    with patch.object(animegan, "MAX_RESULT_BYTES", 2):
        with pytest.raises(ExternalServiceError, match="некорректный"):
            run(setup_responses() + [Response(body=b"123")])


@pytest.mark.parametrize(
    "error", [asyncio.TimeoutError(), aiohttp.ClientConnectionError()]
)
def test_network_failure(error):
    async def fail(file):
        raise error

    with patch.object(animegan, "_convert", fail):
        with pytest.raises(ExternalServiceError):
            asyncio.run(animegan.mask_anime2(io.BytesIO()))


def test_total_timeout():
    async def slow(file):
        await asyncio.sleep(10)

    with (
        patch.object(animegan, "_convert", slow),
        patch.object(animegan, "TIMEOUT", 0.01),
    ):
        with pytest.raises(ExternalServiceError, match="60 секунд"):
            asyncio.run(animegan.mask_anime2(io.BytesIO()))
