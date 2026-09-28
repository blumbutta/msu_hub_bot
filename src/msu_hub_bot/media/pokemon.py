"""Bounded, in-memory Pokémon silhouettes and matching colour reveals."""

from io import BytesIO

from PIL import Image

CANVAS_SIZE = 640
CONTENT_SIZE = 528
BACKGROUND = (248, 248, 248)
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000
MAX_IMAGE_EDGE = 4096


def _decode(image: bytes) -> Image.Image:
    if not image or len(image) > MAX_IMAGE_BYTES:
        raise ValueError("Invalid Pokémon image size")
    try:
        with Image.open(BytesIO(image)) as source:
            if source.format != "PNG" or getattr(source, "n_frames", 1) != 1:
                raise ValueError("Expected a single PNG image")
            width, height = source.size
            if width * height > MAX_IMAGE_PIXELS or max(width, height) > MAX_IMAGE_EDGE:
                raise ValueError("Pokémon image dimensions exceed the limit")
            source.load()
            rgba = source.convert("RGBA")
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError("Invalid Pokémon image") from error
    alpha = rgba.getchannel("A")
    minimum, maximum = alpha.getextrema()
    if minimum != 0 or maximum == 0:
        # An opaque rectangle is not a usable silhouette; an empty image is not a question.
        raise ValueError("Expected a visible Pokémon on a transparent background")
    return rgba


def render_pokemon(image: bytes, *, solution: bool = False) -> bytes:
    """Keep the exact same pose; the question uses alpha only, never source RGB."""
    rgba = _decode(image)
    bounds = rgba.getchannel("A").getbbox()
    if bounds is None:
        raise ValueError("Pokémon image is empty")
    cropped = rgba.crop(bounds)
    scale = CONTENT_SIZE / max(cropped.size)
    size = (max(1, round(cropped.width * scale)), max(1, round(cropped.height * scale)))
    # Resize the alpha independently so changing source colours cannot change a question.
    alpha = cropped.getchannel("A").resize(size, Image.Resampling.LANCZOS)
    foreground = cropped.resize(size, Image.Resampling.LANCZOS).convert("RGB") if solution else Image.new("RGB", size, (0, 0, 0))
    canvas = Image.new("RGB", (CANVAS_SIZE, CANVAS_SIZE), BACKGROUND)
    canvas.paste(foreground, ((CANVAS_SIZE - size[0]) // 2, (CANVAS_SIZE - size[1]) // 2), alpha)
    output = BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()
