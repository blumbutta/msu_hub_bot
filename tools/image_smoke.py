"""Run inside the production image with Docker networking disabled."""

import asyncio
import importlib
import shutil
import socket
from unittest.mock import AsyncMock


def blocked(*args, **kwargs):
    raise RuntimeError("Offline image check attempted network access")


socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked


async def main():
    for program in ("ffmpeg", "ffprobe", "tesseract"):
        assert shutil.which(program), program
    importlib.import_module("cv2")
    from acrcloud.recognizer import ACRCloudRecognizer

    ACRCloudRecognizer({"host": "example.invalid", "access_key": "test", "access_secret": "test", "timeout": 1})
    from msu_hub_bot.settings import settings
    from msu_hub_bot.cli import prepare_imports

    settings.bot_token = "123456789:" + "a" * 35
    settings.redis_host = "localhost"
    settings.edgedb_dsn = "edgedb://localhost/msu_hub"
    prepare_imports()
    app = importlib.import_module("main")
    app.app.on_startup_all = AsyncMock()
    try:
        await app.on_startup(app.dp)
        assert len(app.dp.message_handlers.handlers) == 302
        assert len(app.dp.edited_message_handlers.handlers) == 148
        assert len(app.app.scheduler.get_jobs()) == 1
        from PIL import ImageFont
        from resources import times_new_roman_font

        assert ImageFont.truetype(str(times_new_roman_font), 24).getbbox("Привет, Ёж!")
    finally:
        await app.on_shutdown(app.dp)
    print("Linux image: native libraries, resources, handlers, and shutdown passed")


asyncio.run(main())
