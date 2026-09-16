"""AnimeGANv2's public Gradio API (upload, queue, download)."""

import asyncio
import io
import json
import re
from urllib.parse import quote

import aiohttp
from PIL import Image

from common.externals.exceptions import ExternalServiceError

API = "https://akhaliq-animeganv2.hf.space/gradio_api"
TIMEOUT = 60
MAX_RESULT_BYTES = 10 * 1024 * 1024


def _check_status(response):
    if response.status == 429:
        raise ExternalServiceError(
            "AnimeGAN перегружен или исчерпан лимит запросов. Попробуйте позже."
        )
    if response.status != 200:
        raise ExternalServiceError(
            f"Сервис AnimeGAN недоступен (HTTP {response.status}). Попробуйте позже."
        )


async def _result_events(content):
    event, data = "", []
    async for raw in content:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if event == "error":
                raise ExternalServiceError(
                    "AnimeGAN не смог обработать изображение. Попробуйте позже."
                )
            if event == "complete":
                return json.loads("\n".join(data))
            event, data = "", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    raise ExternalServiceError(
        "AnimeGAN прервал соединение до завершения обработки. Попробуйте позже."
    )


async def _convert(file):
    timeout = aiohttp.ClientTimeout(total=TIMEOUT, connect=15, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        form = aiohttp.FormData()
        form.add_field(
            "files",
            file.getvalue(),
            filename="portrait.png",
            content_type="application/octet-stream",
        )
        async with session.post(f"{API}/upload", data=form) as response:
            _check_status(response)
            uploaded = await response.json()
        if (
            not isinstance(uploaded, list)
            or not uploaded
            or not isinstance(uploaded[0], str)
        ):
            raise ValueError("Invalid upload response")
        payload = {
            "data": [
                {"path": uploaded[0], "meta": {"_type": "gradio.FileData"}},
                "Version 2",
            ]
        }
        async with session.post(f"{API}/call/generate", json=payload) as response:
            _check_status(response)
            submitted = await response.json()
        event_id = submitted.get("event_id")
        if not isinstance(event_id, str) or not re.fullmatch(
            r"[a-zA-Z0-9_-]+", event_id
        ):
            raise ValueError("Invalid event ID")
        async with session.get(f"{API}/call/generate/{event_id}") as response:
            _check_status(response)
            result = await _result_events(response.content)
        path = result[0]["path"]
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("Invalid result path")
        # Download only from the configured Space, never an arbitrary returned URL.
        async with session.get(
            f"{API}/file={quote(path, safe='/')}", allow_redirects=False
        ) as response:
            _check_status(response)
            output = io.BytesIO()
            async for chunk in response.content.iter_chunked(65536):
                if output.tell() + len(chunk) > MAX_RESULT_BYTES:
                    raise ValueError("Result too large")
                output.write(chunk)
        output.seek(0)
        with Image.open(output) as image:
            image.verify()
        output.seek(0)
        return output


async def mask_anime2(file: io.BytesIO) -> io.BytesIO:
    try:
        # Bound the whole operation, including a queue that keeps sending heartbeats.
        return await asyncio.wait_for(_convert(file), timeout=TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise ExternalServiceError(
            "Превышено время ожидания AnimeGAN (до 60 секунд). Попробуйте позже."
        ) from exc
    except aiohttp.ClientError as exc:
        raise ExternalServiceError(
            "Не удалось связаться с AnimeGAN. Попробуйте позже."
        ) from exc
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        OSError,
    ) as exc:
        raise ExternalServiceError(
            "AnimeGAN вернул некорректный результат. Попробуйте позже."
        ) from exc
