"""Run inside the production image with Docker networking disabled."""

import asyncio
import csv
import gzip
import hashlib
import importlib
import importlib.util
import io
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import wave
from contextlib import closing
from pathlib import Path


def blocked(*args, **kwargs):
    raise RuntimeError("Offline image check attempted network access")


def synthetic_audio():
    """Integer-only waveform keeps the native fingerprint contract reproducible."""
    rate = 8000
    samples = [(((index * (109 + 19 * (index // rate))) % 4096) - 2048) * 8 for index in range(rate * 10)]
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(struct.pack("<" + str(len(samples)) + "h", *samples))
    return output.getvalue()


def check_fingerprint(audio):
    from acrcloud.recognizer import ACRCloudRecognizer

    samples = []
    recognizer = ACRCloudRecognizer({"host": "example.invalid", "access_key": "test", "access_secret": "test", "timeout": 1})

    def capture(host, query, *args):
        samples.append(query["sample"])
        return '{"status":{"code":0}}'

    recognizer.do_recogize = capture
    assert json.loads(recognizer.recognize_by_filebuffer(audio, 0))["status"]["code"] == 0
    assert len(samples) == 1 and len(samples[0]) == 3128
    assert hashlib.sha256(samples[0]).hexdigest() == "ca67e27a7c31285581faa7476deb76be3e9bda1fa44df642190059c9bf569c9e"


def check_media(audio):
    from PIL import Image, ImageDraw, ImageFont

    from msu_hub_bot.commands.camera import camera_frame
    from msu_hub_bot.commands.tesseract import to_text
    from msu_hub_bot.media.sticker_media import prepare_static, prepare_video
    from msu_hub_bot.resources import ubuntu_mono_font

    def run(*args):
        return subprocess.run(args, check=True, capture_output=True, timeout=60).stdout

    image = Image.new("RGB", (800, 180), "white")
    ImageDraw.Draw(image).text((30, 35), "ПРИВЕТ МИР 314", font=ImageFont.truetype(str(ubuntu_mono_font), 72), fill="black")
    png = io.BytesIO()
    image.save(png, format="PNG")
    payload = png.getvalue()
    assert to_text(io.BytesIO(payload)).strip() == "ПРИВЕТ МИР 314", "Cyrillic OCR failed"
    sticker = prepare_static(payload)
    with Image.open(io.BytesIO(sticker.payload)) as webp:
        assert webp.format == "WEBP" and max(webp.size) == 512

    with tempfile.TemporaryDirectory(prefix="hub-image-smoke-") as directory:
        root = Path(directory)
        wav, opus, video = root / "audio.wav", root / "audio.ogg", root / "video.avi"
        wav.write_bytes(audio)
        run("ffmpeg", "-nostdin", "-v", "error", "-i", str(wav), "-c:a", "libopus", str(opus))
        probe = json.loads(run("ffprobe", "-v", "error", "-show_streams", "-of", "json", str(opus)))
        assert probe["streams"][0]["codec_name"] == "opus"
        run(
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x96:rate=10:duration=0.4",
            "-i",
            str(wav),
            "-t",
            "0.4",
            "-c:v",
            "mjpeg",
            "-c:a",
            "pcm_s16le",
            "-threads",
            "1",
            str(video),
        )
        frame = camera_frame(str(video))
        assert frame is not None, "FFmpeg could not decode a camera frame"
        with Image.open(io.BytesIO(frame)) as decoded:
            assert decoded.format == "JPEG" and decoded.size == (160, 96)
        converted = prepare_video(video.read_bytes())
        assert converted.kind == "video" and converted.payload and not converted.trimmed
        check_captions(payload, video.read_bytes())


def check_captions(image_payload, video_payload):
    from PIL import Image, ImageFont

    from msu_hub_bot.media.caption_layout import caption_image
    from msu_hub_bot.media.caption_video import caption_video
    from msu_hub_bot.resources import meme_font

    assert ImageFont.truetype(str(meme_font), 24).getbbox("Привет, Ёж!"), "Meme font is missing from the image"
    text = 'Ёж: "всё нормально" [100%] \\ путь.\n' * 48
    with tempfile.TemporaryDirectory(prefix="hub-caption-smoke-") as directory:
        for style in ("lobster", "demotivator", "meme"):
            with io.BytesIO(image_payload) as source, closing(caption_image(source, text, style)) as image:
                assert image.getbbox() is not None, f"{style} image is empty"
                assert image.width >= 320 and image.height >= 240
            with io.BytesIO(video_payload) as source:
                converted = caption_video(source, text, style)
            assert converted is not None, f"{style} video failed"
            output = Path(directory) / f"{style}.mp4"
            with converted:
                output.write_bytes(converted.getvalue())
            probe = json.loads(
                subprocess.run(
                    ["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(output)],
                    check=True,
                    capture_output=True,
                    timeout=30,
                ).stdout
            )
            video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
            assert video["codec_name"] == "h264" and int(video["nb_read_frames"]) == 4, f"{style} video lost frames"
            assert any(stream["codec_name"] == "aac" for stream in probe["streams"]), f"{style} video lost audio"
            assert video["width"] % 2 == video["height"] % 2 == 0
            final_frame = subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-ss",
                    "0.3",
                    "-i",
                    str(output),
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "pipe:1",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            ).stdout
            with Image.open(io.BytesIO(final_frame)) as frame:
                assert frame.size == (video["width"], video["height"]), f"{style} final frame failed to decode"


def check_sticker_animation_formats():
    from PIL import Image

    from msu_hub_bot.media.sticker_media import prepare_media

    frames = [Image.new("RGBA", (32, 32), color) for color in ((255, 0, 0, 128), (0, 255, 0, 128), (0, 0, 255, 128))]
    with tempfile.TemporaryDirectory(prefix="hub-animation-smoke-") as directory:
        for format_name in ("PNG", "WEBP"):
            source = io.BytesIO()
            frames[0].save(source, format=format_name, save_all=True, append_images=frames[1:], duration=[100, 400, 200], loop=0)
            prepared = prepare_media(source.getvalue(), "static")
            assert prepared.kind == "video" and not prepared.trimmed
            output = Path(directory) / "sticker.webm"
            output.write_bytes(prepared.payload)
            decoded = subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-c:v",
                    "libvpx-vp9",
                    "-i",
                    str(output),
                    "-vf",
                    "format=rgba,scale=1:1",
                    "-pix_fmt",
                    "rgba",
                    "-f",
                    "rawvideo",
                    "pipe:1",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            ).stdout
            pixels = [decoded[index : index + 4] for index in range(0, len(decoded), 4)]
            assert 20 <= len(pixels) <= 22, f"{format_name} frame timing changed"
            colors = [max(range(3), key=lambda channel: pixel[channel]) for pixel in pixels]
            assert all(abs(colors.count(color) - count) <= 1 for color, count in ((0, 3), (1, 12), (2, 6)))
            assert all(120 <= pixel[3] <= 136 for pixel in pixels), f"{format_name} transparency lost"


def check_animation():
    from msu_hub_bot.commands.animate import AnimateTextSticker, MatrixSticker, animate

    def has_outline(value):
        if isinstance(value, dict):
            return value.get("ty") == "sh" or any(has_outline(child) for child in value.values())
        return isinstance(value, list) and any(has_outline(child) for child in value)

    for builder in (AnimateTextSticker, MatrixSticker):
        result = animate(builder, "Ёж й")
        assert result is not None
        payload = result.getvalue()
        assert len(payload) < 64 * 1024
        document = json.loads(gzip.decompress(payload))
        assert document["w"] == document["h"] == 512
        assert 0 < (document["op"] - document["ip"]) / document["fr"] <= 3
        assert has_outline(document), "Animated sticker has no rendered glyphs"

    from lottie import NVector, objects
    from lottie.exporters.svg import export_svg
    from lottie.utils.color import Color
    from PIL import Image
    import resvg_py

    from msu_hub_bot.media.sticker_media import prepare_custom_emoji

    animation = objects.Animation(30)
    animation.width = animation.height = 100
    layer = animation.add_layer(objects.ShapeLayer())
    rectangle = layer.add_shape(objects.Rect())
    rectangle.position.value = NVector(50, 50)
    rectangle.size.value = NVector(80, 80)
    layer.add_shape(objects.Fill(Color(1, 0, 0)))
    prepared = prepare_custom_emoji(gzip.compress(json.dumps(animation.to_dict()).encode()), "animated")
    normalized = objects.Animation.load(json.loads(gzip.decompress(prepared.payload)))
    svg = io.BytesIO()
    export_svg(normalized, svg, frame=0, pretty=False)
    rendered = resvg_py.svg_to_bytes(svg_string=svg.getvalue().decode(), skip_system_fonts=True)
    with Image.open(io.BytesIO(rendered)) as image:
        assert image.size == (512, 512)
        assert image.getpixel((256, 256)) == (255, 0, 0, 255)
        assert image.getpixel((0, 0)) == (0, 0, 0, 0)


def check_youtube_runtime():
    from yt_dlp import YoutubeDL
    from yt_dlp.extractor.youtube import YoutubeIE
    from yt_dlp.extractor.youtube.jsc._builtin.deno import DenoJCP
    from yt_dlp.extractor.youtube.jsc._builtin.ejs import ScriptSource
    from yt_dlp.extractor.youtube.jsc.provider import JsChallengeRequest, JsChallengeType, NChallengeInput, SigChallengeInput
    from yt_dlp.extractor.youtube.pot._director import YoutubeIEContentProviderLogger

    with YoutubeDL({"quiet": True, "cachedir": False, "remote_components": [], "js_runtimes": {"deno": {}}}) as downloader:
        extractor = YoutubeIE(downloader)
        provider = DenoJCP(extractor, YoutubeIEContentProviderLogger(extractor, "image-smoke"), {})
        try:
            assert provider.is_available(), "yt-dlp could not find a supported Deno runtime"
            assert provider._lib_script.source is ScriptSource.PYPACKAGE
            assert provider._core_script.source is ScriptSource.PYPACKAGE
            requests = [
                JsChallengeRequest(JsChallengeType.N, NChallengeInput("https://example.invalid/player.js", ["abc"])),
                JsChallengeRequest(JsChallengeType.SIG, SigChallengeInput("https://example.invalid/player.js", ["abc"])),
            ]
            script = provider._construct_stdin(
                '_result.n = value => value + "-ok"; _result.sig = value => value.split("").reverse().join("");',
                True,
                requests,
            )
            result = json.loads(provider._run_js_runtime(script))
            assert result == {
                "type": "result",
                "responses": [{"type": "result", "data": {"abc": "abc-ok"}}, {"type": "result", "data": {"abc": "cba"}}],
            }
        finally:
            provider.close()


def check_chess():
    import chess
    from PIL import Image

    from msu_hub_bot.games.chess_play.models import Game, Player
    from msu_hub_bot.media.chess_play_board import HEIGHT, WIDTH, captured_pieces, render_match
    from msu_hub_bot.media.chessboard import render_board

    board = chess.Board()
    for move in ("e2e4", "e7e5"):
        question = render_board(board.fen())
        solution = render_board(board.fen(), arrow=move)
        assert question != solution, "Chess solution arrow is missing"
        for payload in (question, solution):
            with Image.open(io.BytesIO(payload)) as image:
                assert image.format == "PNG" and image.size == (720, 720)
                image.verify()
        board.push_uci(move)

    match = Game(
        token="a" * 12,
        bot_id=42,
        chat_id=-10012,
        white=Player(user_id=1, name="Белые"),
        created_at=1000,
        invite_deadline=1600,
    )
    match.join(Player(user_id=2, name="Чёрные"), 1100)
    for move in ("e2e4", "d7d5", "e4d5", "d8d5"):
        match.move(match.turn_player.user_id, move, 1100)
    assert captured_pieces(match) == ((chess.Piece(chess.PAWN, chess.BLACK),), (chess.Piece(chess.PAWN, chess.WHITE),))
    active = render_match(match)
    match.resign(match.white.user_id, 1101)
    finished = render_match(match)
    assert active != finished, "Chess match result banner is missing"
    for payload in (active, finished):
        with Image.open(io.BytesIO(payload)) as image:
            assert image.format == "PNG" and image.size == (WIDTH, HEIGHT), "Chess match PNG is invalid"
            image.verify()


def check_background():
    from PIL import Image, ImageDraw
    from msu_hub_bot.media.background import remove_background
    from msu_hub_bot.media.background_model import verified_model

    verified_model()
    with Image.new("RGB", (320, 320), "#f0e8dd") as image, io.BytesIO() as source:
        drawing = ImageDraw.Draw(image)
        drawing.ellipse((110, 35, 210, 135), fill="#bb8666")
        drawing.rectangle((95, 130, 225, 245), fill="#2f718f")
        drawing.rectangle((95, 245, 145, 305), fill="#303040")
        drawing.rectangle((175, 245, 225, 305), fill="#303040")
        image.save(source, "PNG")
        result = remove_background(source)
    with Image.open(io.BytesIO(result)) as cutout:
        assert cutout.mode == "RGBA" and cutout.size == (320, 320)
        assert cutout.getpixel((160, 170))[3] >= 240, "Foreground disappeared"
        assert cutout.getpixel((5, 5))[3] <= 10, "Background remains opaque"


async def check_membership_journal():
    from unittest.mock import AsyncMock

    from msu_hub_bot.storage.models import MembershipBatch
    from msu_hub_bot.telegram.membership_inbox import MembershipInbox

    directory = Path("/data")
    assert directory.stat().st_uid == os.geteuid() == 10001
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    with tempfile.TemporaryDirectory(prefix="membership-smoke-", dir=directory) as temporary:
        path = Path(temporary) / "inbox.sqlite3"
        inbox = MembershipInbox(path, 999, AsyncMock())
        await inbox.open()
        await inbox.enqueue([MembershipBatch(update_id=123)])
        await inbox.close()
        replay = MembershipInbox(path, 999, AsyncMock())
        await replay.open()
        assert await replay.pending() == 1, "Membership journal did not survive reopening"
        await replay.close()


async def main():
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.getaddrinfo = blocked
    for package in ("common", "hub_bot", "edgedb", "gel", "cv2", "redis", "hiredis"):
        assert importlib.util.find_spec(package) is None, f"Retired package is installed: {package}"
    for program in ("ffmpeg", "ffprobe", "tesseract"):
        assert shutil.which(program), program
    audio = synthetic_audio()
    check_fingerprint(audio)
    check_media(audio)
    check_sticker_animation_formats()
    check_animation()
    check_youtube_runtime()
    check_chess()
    check_background()
    await check_membership_journal()
    from msu_hub_bot.web import server as web_server

    static = Path(web_server.__file__).with_name("static")
    assert (static / "index.html").is_file(), "Mini App HTML missing from image"
    assert list((static / "assets").glob("*.js")), "Mini App bundle missing from image"
    from msu_hub_bot.app import Application
    from msu_hub_bot.settings import settings

    settings.bot_token = "123456789:" + "a" * 35
    settings.storage_backend = "supabase"
    settings.supabase_url = "https://database.example.invalid"
    settings.supabase_key = "synthetic-publishable-key"
    settings.supabase_email = "bot@example.invalid"
    settings.supabase_password = "synthetic-password"
    settings.jev_enabled = True
    settings.openrouter_api_key = "synthetic-openrouter-key"
    app = await Application.create(settings)
    try:

        def count(event):
            return sum(len(router.observers[event].handlers) for router in app.dispatcher.chain_tail)

        assert count("message") == 275
        assert count("callback_query") == 26
        assert count("edited_message") == 149
        assert count("inline_query") == count("chosen_inline_result") == 0
        for event, key in (
            ("message", "Feedback.process"),
            ("callback_query", "Feedback.process_cb"),
            ("message", "Art.process"),
            ("message", "Art.top"),
            ("callback_query", "Art.process_cb"),
        ):
            assert any(
                handler.flags.get("handler_key") == key
                for router in app.dispatcher.chain_tail
                for handler in router.observers[event].handlers
            ), key
        from PIL import ImageFont

        from msu_hub_bot.execution.sed import sed_calc
        from msu_hub_bot.providers.vk.posts import VkPost
        from msu_hub_bot.resources import debate, lobster_font, times_new_roman_font, ubuntu_mono_font

        for font in (lobster_font, times_new_roman_font, ubuntu_mono_font):
            assert ImageFont.truetype(str(font), 24).getbbox("Привет, Ёж!"), font.name
        with debate.open(encoding="utf-8", newline="") as source:
            rows = csv.reader(source, delimiter=";")
            assert len(next(rows)) == 7
            row = next(rows)
            assert len(row) == 7 and row[-1].strip()
        post = VkPost({"id": 1, "owner_id": -1, "date": 0, "text": "Текст <example> &", "attachments": []}, {})
        assert post.render(with_header=False) == "Текст &lt;example&gt; &amp;"
        assert await asyncio.to_thread(sed_calc, "Привет, кот!", ["s/кот/бот/"]) == "Привет, бот!"
    finally:
        await app.close()
    print(
        "Linux image: fingerprint, OCR, camera, image/video captions, Opus/VP9/WebP/TGS, chess PNG, offline foreground masks, Deno/EJS, resources, worker, handlers, and shutdown passed"
    )


if __name__ == "__main__":
    asyncio.run(main())
