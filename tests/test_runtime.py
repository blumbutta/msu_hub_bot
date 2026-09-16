import ast
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from msu_hub_bot.cli import prepare_imports
from msu_hub_bot.settings import MissingIntegration, settings

ROOT = Path(__file__).resolve().parents[1]


def test_registration_source_matches_command_inventory():
    inventory = json.loads((ROOT / "tests/fixtures/handler_inventory.json").read_text())
    tree = ast.parse((ROOT / "hub_bot/main.py").read_text())
    calls = sorted(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr.startswith("register_")
            and ast.unparse(n.func.value) == "dp"
        ),
        key=lambda n: n.lineno,
    )
    assert [(c.func.attr, ast.unparse(c.args[0].func if isinstance(c.args[0], ast.Call) else c.args[0])) for c in calls] == [
        (item["type"], item["handler"]) for item in inventory
    ]
    for call, item in zip(calls, inventory):
        aliases = []
        for value in [*call.args[1:], *(k.value for k in call.keywords if k.arg == "commands")]:
            if isinstance(value, ast.Call) and ast.unparse(value.func) == "MetaCommand":
                aliases.extend(n.value for n in value.args if isinstance(n, ast.Constant) and isinstance(n.value, str))
            elif isinstance(value, (ast.List, ast.Tuple)):
                aliases.extend(n.value for n in value.elts if isinstance(n, ast.Constant) and isinstance(n.value, str))
        assert aliases == item["aliases"]


@pytest.mark.asyncio
async def test_offline_startup_handlers_shutdown_and_demotivator(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "bot_token", "123456789:" + "a" * 35)
    monkeypatch.setattr(settings, "redis_host", "127.0.0.1")
    monkeypatch.setattr(settings, "edgedb_dsn", "edgedb://localhost/msu_hub")
    monkeypatch.setattr(settings, "logs_file", str(tmp_path / "{name}.log"))
    prepare_imports()
    import main
    from commands import lobster
    from utils.jdoodle import LANGUAGES

    monkeypatch.setattr(main.app, "on_startup_all", AsyncMock())
    try:
        await main.on_startup(main.dp)
        expected = {
            "message_handlers": 302,
            "callback_query_handlers": 21,
            "edited_message_handlers": 148,
            "channel_post_handlers": 3,
            "inline_query_handlers": 1,
            "errors_handlers": 1,
        }
        assert {kind: len(getattr(main.dp, kind).handlers) for kind in expected} == expected
        assert len(LANGUAGES) == 70
        assert main.redis.generate_key("bot", "to_delete") == "hub:bot:to_delete"
        assert len(main.app.scheduler.get_jobs()) == 1  # VK posting remains disabled.
        with pytest.raises(MissingIntegration):
            _ = main.app.jdoodle.instance
        message = SimpleNamespace(reply=AsyncMock(), chat=SimpleNamespace(type="private"))
        update = SimpleNamespace(callback_query=None, message=message)
        await main.process_error(update, MissingIntegration("wolfram_token"))
        assert "не настроена" in message.reply.call_args.args[0]

        @asynccontextmanager
        async def no_chat_action(*args, **kwargs):
            yield

        monkeypatch.setattr(lobster, "ChatActioner", no_chat_action)
        source = io.BytesIO()
        Image.new("RGB", (480, 320), "#507b87").save(source, "PNG")
        source.seek(0)
        target = SimpleNamespace(reply_photo=AsyncMock())
        meta = SimpleNamespace(
            extract_video=AsyncMock(return_value=(target, None)),
            extract_image_with_downloading=AsyncMock(return_value=(target, source)),
            extract_text=lambda: (target, "Наследие живёт\nLegacy lives on"),
        )
        await lobster.process_demotivator(message, meta)
        rendered = target.reply_photo.call_args.args[0]
        image = Image.open(rendered)
        assert image.width > 480 and image.height > 380
        assert image.getpixel((0, 0)) == (0, 0, 0)
        assert any(pixel != (0, 0, 0) for pixel in image.crop((0, 370, image.width, image.height)).getdata())
        assert "LiberationSerif-Regular.ttf" in next(lobster.drawtexts("Привет", 480))
        image.save(tmp_path / "demotivator.png")
    finally:
        await main.on_shutdown(main.dp)


@pytest.mark.asyncio
async def test_redis_deletion_state_format(monkeypatch):
    from common.tg.storage import RedisStorage

    store = RedisStorage(host="localhost", prefix="hub")
    monkeypatch.setattr(store, "dict_set", AsyncMock(return_value=True))
    monkeypatch.setattr("common.tg.storage.time.time", lambda: 1_000)
    try:
        await store.mark_message_to_delete_raw(-101, 202, 30)
        store.dict_set.assert_awaited_once_with("hub:bot:to_delete", "-101_202", "1030")
    finally:
        await store.close()


def test_health_requires_recent_successful_poll(monkeypatch, tmp_path):
    from msu_hub_bot.health import heartbeat_path, mark_poll_success, ready

    monkeypatch.setenv("HUB_POLL_HEARTBEAT", str(tmp_path / "poll"))
    monkeypatch.setattr("msu_hub_bot.health.time.monotonic", lambda: 1_000)
    assert not ready()
    mark_poll_success()
    assert ready()
    heartbeat_path().write_text("800")
    assert not ready()
