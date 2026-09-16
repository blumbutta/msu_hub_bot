"""Local media preparation; no Telegram calls or credentials."""

import gzip
import io
import json
import math
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageOps

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 256 * 1024


class StickerMediaError(ValueError):
    pass


def speed_factor(duration):
    if not math.isfinite(duration) or duration <= 0:
        raise StickerMediaError("Не удалось определить длительность анимации.")
    if duration > 7:
        raise StickerMediaError("Анимация длиннее 7 секунд. Стикер не добавлен.")
    # Leave a frame-sized margin for container timestamp rounding.
    return duration / 2.95 if duration > 3 else 1.0


def _run(command):
    try:
        return subprocess.run(command, check=True, capture_output=True, timeout=60).stdout
    except FileNotFoundError as exc:
        raise StickerMediaError("На сервере нужны FFmpeg и FFprobe.") from exc
    except subprocess.TimeoutExpired as exc:
        raise StickerMediaError("Обработка заняла слишком много времени. Стикер не добавлен.") from exc
    except subprocess.CalledProcessError as exc:
        raise StickerMediaError("Не удалось прочитать или преобразовать анимацию.") from exc


def _probe(path):
    info = json.loads(
        _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise StickerMediaError("В файле нет видео.")
    duration = video.get("duration") or info.get("format", {}).get("duration")
    try:
        duration = float(duration)
    except (TypeError, ValueError) as exc:
        raise StickerMediaError("Не удалось определить длительность анимации.") from exc
    return info, video, duration


def prepare_video(data):
    """Convert one playback of GIF/video to a silent VP9 sticker, never truncate."""
    if len(data) > MAX_INPUT_BYTES:
        raise StickerMediaError("Файл больше 20 МБ. Стикер не добавлен.")
    with TemporaryDirectory(prefix="hub-sticker-") as directory:
        source = Path(directory) / "source"
        output = Path(directory) / "sticker.webm"
        source.write_bytes(data)
        _, video, duration = _probe(source)
        factor = speed_factor(duration)
        # Preserve alpha when decoding existing VP9 video stickers.
        decoder = ["-c:v", "libvpx-vp9"] if video.get("codec_name") == "vp9" else []
        # Preserve display proportions before changing the pixel aspect ratio to 1.
        filters = (
            f"setpts=(PTS-STARTPTS)/{factor:.12f},"
            "scale=w='if(gte(dar,1),512,max(2,trunc(512*dar/2)*2))':"
            "h='if(gte(dar,1),max(2,trunc(512/dar/2)*2),512)',"
            "setsar=1,fps=30:round=down,format=yuva420p"
        )
        _run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                *decoder,
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                filters,
                "-c:v",
                "libvpx-vp9",
                "-b:v",
                "0",
                "-crf",
                "32",
                "-deadline",
                "good",
                "-cpu-used",
                "4",
                "-threads",
                "2",
                "-auto-alt-ref",
                "0",
                str(output),
            ]
        )
        if output.stat().st_size <= MAX_VIDEO_BYTES:
            info, result, length = _probe(output)
            width, height = result["width"], result["height"]
            fps_num, fps_den = result.get("avg_frame_rate", "0/1").split("/")
            fps = float(fps_num) / float(fps_den or 1)
            if (
                not 0 < length <= 3
                or max(width, height) != 512
                or min(width, height) <= 0
                or not 0 < fps <= 30
                or result.get("codec_name") != "vp9"
                or any(s.get("codec_type") == "audio" for s in info["streams"])
            ):
                raise StickerMediaError("Результат не соответствует ограничениям Telegram.")
            return output.read_bytes()
        raise StickerMediaError("После одной попытки сжатия стикер превышает 256 КБ. Стикер не добавлен.")


def prepare_static(data):
    with Image.open(io.BytesIO(data)) as source:
        if getattr(source, "is_animated", False):
            return "video", prepare_video(data)
        image = ImageOps.exif_transpose(source).convert("RGBA")
        ratio = 512 / max(image.size)
        size = tuple(max(1, round(value * ratio)) for value in image.size)
        image = image.resize(size, Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="WEBP", lossless=True)
        if output.tell() > 512 * 1024:
            raise StickerMediaError("После одной попытки сжатия картинка превышает 512 КБ. Стикер не добавлен.")
        return "static", output.getvalue()


def prepare_tgs(data):
    # Only existing Telegram TGS stickers are accepted, not arbitrary Lottie files.
    if len(data) > 64 * 1024:
        raise StickerMediaError("TGS-стикер больше 64 КБ.")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as source:
            unpacked = source.read(2 * 1024 * 1024 + 1)
        if len(unpacked) > 2 * 1024 * 1024:
            raise ValueError("oversized TGS")
        animation = json.loads(unpacked)
        duration = (float(animation["op"]) - float(animation["ip"])) / float(animation["fr"])
        speed_factor(duration)
        if duration > 3:
            # Existing valid Telegram TGS stickers already have a <=3s timeline.
            # Retiming arbitrary Lottie requires handling precompositions and time remapping.
            raise StickerMediaError("Некорректный TGS-стикер: длительность больше 3 секунд. Пришлите GIF или видео.")
    except StickerMediaError:
        raise
    except (OSError, EOFError, ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
        raise StickerMediaError("Не удалось прочитать TGS-стикер.") from exc
    return data


def prepare_media(data, kind):
    if len(data) > MAX_INPUT_BYTES:
        raise StickerMediaError("Файл больше 20 МБ. Стикер не добавлен.")
    if kind == "animated":
        return "animated", prepare_tgs(data)
    if kind == "video":
        return "video", prepare_video(data)
    try:
        return prepare_static(data)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        if isinstance(exc, StickerMediaError):
            raise
        raise StickerMediaError("Не удалось прочитать картинку.") from exc
