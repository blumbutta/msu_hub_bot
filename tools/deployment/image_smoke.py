"""Run inside the production image with Docker networking disabled."""

import asyncio
import csv
import gzip
import hashlib
import importlib
import importlib.util
import io
import json
import shutil
import socket
import struct
import subprocess
import tempfile
import wave
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
    png.seek(0)
    assert to_text(png).strip() == "ПРИВЕТ МИР 314", "Cyrillic OCR failed"
    sticker = prepare_static(png.getvalue())
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
            "-c:v",
            "mjpeg",
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


async def main():
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.getaddrinfo = blocked
    for package in ("common", "hub_bot", "edgedb", "gel", "cv2", "numpy"):
        assert importlib.util.find_spec(package) is None, f"Retired package is installed: {package}"
    for program in ("ffmpeg", "ffprobe", "tesseract"):
        assert shutil.which(program), program
    audio = synthetic_audio()
    check_fingerprint(audio)
    check_media(audio)
    check_animation()
    check_youtube_runtime()
    check_chess()
    from msu_hub_bot.app import Application
    from msu_hub_bot.settings import settings

    settings.bot_token = "123456789:" + "a" * 35
    settings.redis_host = "localhost"
    settings.storage_backend = "supabase"
    settings.supabase_url = "https://database.example.invalid"
    settings.supabase_key = "synthetic-publishable-key"
    settings.supabase_email = "bot@example.invalid"
    settings.supabase_password = "synthetic-password"
    app = await Application.create(settings)
    try:

        def count(event):
            return sum(len(router.observers[event].handlers) for router in app.dispatcher.chain_tail)

        assert count("message") == 264
        assert count("callback_query") == 20
        assert count("edited_message") == 149
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
    print("Linux image: fingerprint, OCR, camera, Opus/VP9/WebP/TGS, chess PNG, Deno/EJS, resources, worker, handlers, and shutdown passed")


if __name__ == "__main__":
    asyncio.run(main())
