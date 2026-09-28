"""Rendering hides every RGB detail while retaining a bounded, matching pose."""

from io import BytesIO

import pytest
from PIL import Image, ImageChops, ImageDraw, PngImagePlugin

from msu_hub_bot.media import pokemon
from msu_hub_bot.media.pokemon import BACKGROUND, CANVAS_SIZE, CONTENT_SIZE, render_pokemon


def encoded(image, *, format="PNG", **kwargs):
    output = BytesIO()
    image.save(output, format=format, **kwargs)
    return output.getvalue()


def figure(size=(100, 80)):
    image = Image.new("RGBA", size, (198, 83, 17, 0))
    draw = ImageDraw.Draw(image)
    width, height = size
    draw.ellipse((width // 4, height // 4, width * 3 // 4, height * 3 // 4), fill=(255, 38, 19, 255))
    draw.rectangle((width // 3, height // 8, width // 2, height // 2), fill=(15, 35, 240, 255))
    draw.rectangle((width // 2, height // 2, width * 3 // 4, height * 3 // 4), fill=(0, 245, 15, 128))
    return image


def decoded(data):
    image = Image.open(BytesIO(data))
    image.load()
    return image


def test_source_rgb_and_metadata_cannot_reveal_the_answer():
    first = figure()
    second = Image.new("RGBA", first.size, (30, 243, 228, 255))
    second.putalpha(first.getchannel("A"))
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Description", "Pikachu; PRIVATE_SYNTHETIC_DETAIL")
    silhouette = render_pokemon(encoded(first, pnginfo=metadata))
    assert silhouette == render_pokemon(encoded(second))
    result = decoded(silhouette)
    assert result.mode == "RGB"
    assert result.size == (CANVAS_SIZE, CANVAS_SIZE)
    assert result.getpixel((CANVAS_SIZE // 2, CANVAS_SIZE // 3)) == (0, 0, 0)
    assert result.getpixel((0, 0)) == BACKGROUND
    assert all(r == g == b for r, g, b in result.get_flattened_data())
    assert "Description" not in result.info
    assert b"Pikachu" not in silhouette and b"PRIVATE_SYNTHETIC_DETAIL" not in silhouette


def test_reveal_retains_source_colours_and_exact_same_placement():
    raw = encoded(figure())
    hidden = decoded(render_pokemon(raw))
    revealed = decoded(render_pokemon(raw, solution=True))
    background = Image.new("RGB", hidden.size, BACKGROUND)
    hidden_box = ImageChops.difference(hidden, background).getbbox()
    revealed_box = ImageChops.difference(revealed, background).getbbox()
    assert hidden_box == revealed_box
    assert max(hidden_box[2] - hidden_box[0], hidden_box[3] - hidden_box[1]) == CONTENT_SIZE
    assert any(r > 200 and g < 80 and b < 80 for r, g, b in revealed.get_flattened_data())
    assert any(b > 200 and r < 80 and g < 80 for r, g, b in revealed.get_flattened_data())
    assert hidden.tobytes() != revealed.tobytes()
    assert len(render_pokemon(raw, solution=True)) < 5 * 1024 * 1024


@pytest.mark.parametrize("size", [(4, 4), (20, 200), (300, 20), (1800, 1800)])
@pytest.mark.parametrize("solution", [False, True])
def test_varied_source_sizes_fit_the_same_fixed_canvas(size, solution):
    output = decoded(render_pokemon(encoded(figure(size)), solution=solution))
    assert output.size == (CANVAS_SIZE, CANVAS_SIZE)
    bounds = ImageChops.difference(output, Image.new("RGB", output.size, BACKGROUND)).getbbox()
    assert bounds is not None
    assert min(bounds[:2]) >= (CANVAS_SIZE - CONTENT_SIZE) // 2
    assert max(bounds[2:]) <= (CANVAS_SIZE + CONTENT_SIZE) // 2


def test_palette_transparency_is_supported():
    image = Image.new("P", (20, 20), 0)
    image.putpalette([255, 255, 255, 255, 50, 20] + [0] * 762)
    ImageDraw.Draw(image).rectangle((5, 5, 14, 14), fill=1)
    result = decoded(render_pokemon(encoded(image, transparency=0)))
    assert result.getpixel((0, 0)) == BACKGROUND
    assert result.getpixel((320, 320)) == (0, 0, 0)


@pytest.mark.parametrize(
    "image",
    [Image.new("RGB", (30, 30), "white"), Image.new("RGBA", (30, 30), (10, 20, 30, 255)), Image.new("RGBA", (30, 30))],
)
def test_opaque_and_empty_images_are_rejected(image):
    with pytest.raises(ValueError, match="transparent background"):
        render_pokemon(encoded(image))


@pytest.mark.parametrize("image", [b"", b"not an image", b"\x89PNG\r\n\x1a\ntruncated", encoded(figure())[:100]])
def test_invalid_or_truncated_input_is_rejected(image):
    with pytest.raises(ValueError):
        render_pokemon(image)


def test_non_png_and_animated_png_are_rejected():
    with pytest.raises(ValueError, match="single PNG"):
        render_pokemon(encoded(figure().convert("RGB"), format="JPEG"))
    animated = encoded(figure(), save_all=True, append_images=[Image.new("RGBA", (100, 80), (255, 0, 0, 100))], duration=100)
    with pytest.raises(ValueError, match="single PNG"):
        render_pokemon(animated)


def test_limits_are_checked_before_loading_pixels(monkeypatch):
    raw = encoded(figure())
    monkeypatch.setattr(pokemon, "MAX_IMAGE_BYTES", len(raw) - 1)
    with pytest.raises(ValueError, match="size"):
        render_pokemon(raw)
    monkeypatch.setattr(pokemon, "MAX_IMAGE_BYTES", len(raw))
    monkeypatch.setattr(pokemon, "MAX_IMAGE_PIXELS", 7999)
    with pytest.raises(ValueError, match="dimensions"):
        render_pokemon(raw)
    monkeypatch.setattr(pokemon, "MAX_IMAGE_PIXELS", 8000)
    monkeypatch.setattr(pokemon, "MAX_IMAGE_EDGE", 99)
    with pytest.raises(ValueError, match="dimensions"):
        render_pokemon(raw)


def test_maximum_pixels_are_inclusive(monkeypatch):
    monkeypatch.setattr(pokemon, "MAX_IMAGE_PIXELS", 8000)
    assert decoded(render_pokemon(encoded(figure()))).size == (CANVAS_SIZE, CANVAS_SIZE)
