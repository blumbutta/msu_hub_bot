"""Offline tests: no bot token, Telegram connection or sticker mutations."""

import ast
import asyncio
import gzip
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sticker_media", ROOT / "hub_bot/utils/sticker_media.py")
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)


@pytest.fixture
def handlers():
    # Load the actual module, substituting only unrelated application imports.
    source = (ROOT / "hub_bot/commands/sticker.py").read_text()
    tree = ast.parse(source)
    tree.body = [n for n in tree.body if not isinstance(n, ast.ImportFrom) or not n.module.startswith(("common.", "utils."))]

    async def download(file):
        return io.BytesIO(file.data)

    ns = dict(
        MetaInfo=object,
        MAX_INPUT_BYTES=media.MAX_INPUT_BYTES,
        StickerMediaError=media.StickerMediaError,
        prepare_media=media.prepare_media,
        download=download,
        extract_image=AsyncMock(return_value=(None, None)),
        image_bytes_io=None,
        FakeBytesIO=io.BytesIO,
    )
    exec(compile(tree, "<sticker>", "exec"), ns)
    return ns


@pytest.mark.parametrize("duration,accelerated", [(0.1, False), (3, False), (3.001, True), (6, True), (7, True)])
def test_speed_boundaries(duration, accelerated):
    speed = media.speed_factor(duration)
    assert (speed > 1) == accelerated
    assert duration / speed <= 3


@pytest.mark.parametrize("duration", [0, -1, 7.001, 8, float("nan"), float("inf")])
def test_invalid_duration(duration):
    with pytest.raises(media.StickerMediaError):
        media.speed_factor(duration)


@pytest.mark.parametrize("size", [(640, 480), (480, 640), (1, 1024)])
def test_static_dimensions(size):
    source = io.BytesIO()
    Image.new("RGBA", size, (255, 0, 0, 128)).save(source, format="PNG")
    kind, data = media.prepare_media(source.getvalue(), "static")
    image = Image.open(io.BytesIO(data))
    assert kind == "static"
    assert max(image.size) == 512 and min(image.size) > 0
    assert image.getpixel((0, 0))[3] == 128


def test_tgs_roundtrip():
    data = gzip.compress(json.dumps(dict(ip=0, op=180, fr=60)).encode())
    assert media.prepare_media(data, "animated") == ("animated", data)


@pytest.mark.parametrize("data", [b"invalid", gzip.compress(b"{}")])
def test_bad_tgs(data):
    with pytest.raises(media.StickerMediaError):
        media.prepare_tgs(data)


def test_reject_before_ffmpeg(monkeypatch):
    monkeypatch.setattr(media, "_probe", lambda path: ({}, {"codec_name": "h264"}, 7.001))
    calls = []
    monkeypatch.setattr(media, "_run", lambda command: calls.append(command))
    with pytest.raises(media.StickerMediaError, match="7 секунд"):
        media.prepare_video(b"video")
    assert not calls


def test_video_conversion_command(monkeypatch):
    probes = iter(
        [
            ({}, {"codec_name": "h264"}, 6),
            ({"streams": [{"codec_type": "video"}]}, {"codec_name": "vp9", "width": 512, "height": 288, "avg_frame_rate": "30/1"}, 2.967),
        ]
    )
    monkeypatch.setattr(media, "_probe", lambda path: next(probes))
    commands = []

    def run(command):
        commands.append(command)
        Path(command[-1]).write_bytes(b"encoded")

    monkeypatch.setattr(media, "_run", run)
    assert media.prepare_video(b"source") == b"encoded"
    command = commands[0]
    assert "-an" in command and "-t" not in command
    assert "setpts=(PTS-STARTPTS)/2.033898" in command[command.index("-vf") + 1]


def message(admin=True, kind="static"):
    sticker = SimpleNamespace(
        file_id="input",
        file_size=10,
        data=b"data",
        type="regular",
        is_animated=kind == "animated",
        is_video=kind == "video",
        set_name="with_love_for_100_by_msu_hub_bot",
        delete_from_set=AsyncMock(),
    )
    target = SimpleNamespace(sticker=sticker, animation=None, video=None, video_note=None, photo=[], document=None)
    chat = SimpleNamespace(
        id=-100,
        type="supergroup",
        all_members_are_administrators=False,
        get_administrators=AsyncMock(return_value=[SimpleNamespace(user=SimpleNamespace(id=1 if admin else 2))]),
    )
    return SimpleNamespace(
        chat=chat,
        from_user=SimpleNamespace(id=1),
        sender_chat=None,
        reply_to_message=target,
        sticker=None,
        animation=None,
        video=None,
        video_note=None,
        photo=[],
        document=None,
        reply=AsyncMock(),
        reply_sticker=AsyncMock(),
        bot=SimpleNamespace(request=AsyncMock(return_value={"file_id": "uploaded"}), get_sticker_set=AsyncMock()),
    )


@pytest.mark.parametrize("kind", ["static", "animated", "video"])
def test_source_types(handlers, kind):
    source, actual = handlers["Stickers"].source_media(message(kind=kind))
    assert actual == kind and source.file_id == "input"


@pytest.mark.parametrize("kind", ["static", "animated", "video"])
def test_delete_all_formats(handlers, kind):
    m = message(kind=kind)
    asyncio.run(handlers["process_sticker_delete"](m))
    m.reply_to_message.sticker.delete_from_set.assert_awaited_once()


def test_delete_non_admin(handlers):
    m = message(admin=False, kind="video")
    asyncio.run(handlers["process_sticker_delete"](m))
    m.reply_to_message.sticker.delete_from_set.assert_not_awaited()


def test_delete_without_set(handlers):
    m = message()
    m.reply_to_message.sticker.set_name = None
    asyncio.run(handlers["process_sticker_delete"](m))
    m.reply_to_message.sticker.delete_from_set.assert_not_awaited()


def test_long_video_never_uploaded(handlers):
    m = message(kind="video")

    def reject(*args):
        raise media.StickerMediaError("Анимация длиннее 7 секунд.")

    handlers["prepare_media"] = reject
    asyncio.run(handlers["process_sticker_chat"](m, None, None))
    m.bot.request.assert_not_awaited()
    assert "7 секунд" in m.reply.call_args.args[0]


def test_non_admin_cannot_add(handlers):
    m = message(admin=False)
    asyncio.run(handlers["process_sticker_chat"](m, None, None))
    m.bot.request.assert_not_awaited()


@pytest.mark.parametrize("kind", ["static", "animated", "video"])
def test_upload_and_add_modern_api(handlers, kind):
    m = message(kind=kind)
    handlers["prepare_media"] = lambda data, kind: (kind, b"prepared")
    meta = SimpleNamespace(extract_text=lambda: (m, "😎"))
    asyncio.run(handlers["process_sticker_chat"](m, meta, None))
    calls = m.bot.request.call_args_list
    assert [c.args[0] for c in calls] == ["uploadStickerFile", "addStickerToSet"]
    assert json.loads(calls[1].args[1]["sticker"]) == dict(sticker="uploaded", format=kind, emoji_list=["😎"])


def test_new_pack_preserves_prepared_video(handlers):
    m = message(kind="video")
    m.bot.get_sticker_set.side_effect = handlers["aiogram"].exceptions.InvalidStickersSet("invalid")
    handlers["prepare_media"] = lambda data, kind: (kind, b"prepared")
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock(), finish=AsyncMock())
    meta = SimpleNamespace(extract_text=lambda: (m, ""))
    asyncio.run(handlers["process_sticker_chat"](m, meta, state))
    data = state.update_data.call_args.kwargs
    assert data["mixed_sticker"]["format"] == "video"
    m.text = "Наш пак"
    m.caption = None
    m.bot.request.reset_mock()
    asyncio.run(handlers["Stickers"].finish_chat_set(m, state, data))
    call = m.bot.request.call_args
    assert call.args[0] == "createNewStickerSet"
    assert json.loads(call.args[1]["stickers"])[0]["sticker"] == "uploaded"
    state.finish.assert_awaited_once()


def test_creation_error_keeps_pending_state(handlers):
    m = message()
    m.text = "Пак"
    m.caption = None
    m.bot.get_sticker_set.side_effect = handlers["aiogram"].exceptions.InvalidStickersSet("invalid")
    m.bot.request.side_effect = handlers["aiogram"].exceptions.BadRequest("failure")
    state = SimpleNamespace(finish=AsyncMock())
    data = dict(sticker_chat_id=-100, sticker_user_id=1, sticker_set_name="pack", mixed_sticker={})
    with pytest.raises(handlers["aiogram"].exceptions.BadRequest):
        asyncio.run(handlers["Stickers"].finish_chat_set(m, state, data))
    state.finish.assert_not_awaited()


def test_delete_other_chat_pack(handlers):
    m = message(kind="video")
    m.reply_to_message.sticker.set_name = "with_love_for_999_by_msu_hub_bot"
    asyncio.run(handlers["process_sticker_delete"](m))
    m.reply_to_message.sticker.delete_from_set.assert_not_awaited()


def test_revoked_admin_cannot_finish_pack(handlers):
    m = message(admin=False)
    m.text = "Пак"
    m.caption = None
    state = SimpleNamespace(finish=AsyncMock())
    data = dict(sticker_chat_id=-100, sticker_user_id=1, sticker_set_name="pack", mixed_sticker={})
    asyncio.run(handlers["Stickers"].finish_chat_set(m, state, data))
    m.bot.request.assert_not_awaited()


def test_creation_reuses_concurrently_created_pack(handlers):
    m = message()
    m.text = "Пак"
    m.caption = None
    state = SimpleNamespace(finish=AsyncMock())
    data = dict(
        sticker_chat_id=-100,
        sticker_user_id=1,
        sticker_set_name="pack",
        mixed_sticker={"sticker": "file", "format": "video", "emoji_list": ["✨"]},
    )
    asyncio.run(handlers["Stickers"].finish_chat_set(m, state, data))
    assert m.bot.request.call_args.args[0] == "addStickerToSet"
    state.finish.assert_awaited_once()
