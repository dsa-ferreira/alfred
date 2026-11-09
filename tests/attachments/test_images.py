"""Images checked on arrival and normalised for the model (design D6)."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from alfred.attachments import Unreadable
from alfred.attachments.images import for_model, for_model_async, inspect_image
from tests.attachments.files import image_bytes


def opened(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


@pytest.mark.parametrize(
    ("fmt", "mime"),
    [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp"), ("GIF", "image/gif")],
)
def test_accepted_formats(fmt: str, mime: str) -> None:
    info = inspect_image(image_bytes(40, 30, fmt))
    assert (info.mime, info.width, info.height) == (mime, 40, 30)


def test_large_photo() -> None:
    original = image_bytes(4000, 3000)
    kept = bytes(original)
    assert inspect_image(original).width == 4000
    image = for_model(original, 1536)
    assert image.mime == "image/jpeg"
    assert opened(image.data).size == (1536, 1152)
    assert original == kept  # the stored file is untouched
    assert opened(original).size == (4000, 3000)


def test_small_image_not_enlarged() -> None:
    assert opened(for_model(image_bytes(300, 200), 1536).data).size == (300, 200)


def test_rotated_exif_photo_is_turned_upright() -> None:
    # Stored 300x200 and tagged "rotate 90° clockwise": shown 200x300.
    data = image_bytes(300, 200, orientation=6)
    info = inspect_image(data)
    assert (info.width, info.height) == (200, 300)
    assert opened(for_model(data, 1536).data).size == (200, 300)


def test_transparent_png_stays_png() -> None:
    data = image_bytes(50, 50, "PNG", mode="RGBA", color=(0, 0, 255, 100))
    image = for_model(data, 1536)
    assert image.mime == "image/png"
    assert opened(image.data).mode == "RGBA"


def test_opaque_png_becomes_jpeg() -> None:
    data = image_bytes(50, 50, "PNG", mode="RGBA", color=(0, 0, 255, 255))
    assert for_model(data, 1536).mime == "image/jpeg"


def test_truncated_file_refused() -> None:
    data = image_bytes(400, 300)
    with pytest.raises(Unreadable):
        inspect_image(data[: len(data) // 2])


def test_not_an_image_refused() -> None:
    with pytest.raises(Unreadable):
        inspect_image(b"%PDF-1.4 not an image")


def test_unsupported_format_refused() -> None:
    with pytest.raises(Unreadable, match="BMP"):
        inspect_image(image_bytes(10, 10, "BMP"))


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_too_many_pixels_refused() -> None:
    # Header-only check: a huge, blank PNG compresses to almost nothing.
    with pytest.raises(Unreadable):
        inspect_image(image_bytes(9000, 8000, "PNG", mode="L", color=(0,)))


async def test_runs_in_a_thread() -> None:
    image = await for_model_async(image_bytes(4000, 3000), 1536)
    assert opened(image.data).size == (1536, 1152)
