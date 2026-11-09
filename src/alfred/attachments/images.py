"""Images: checked on arrival, normalised on the way to the model (design D6).

On arrival the file is decoded in full, so a damaged or truncated image is
refused before it is stored. The stored file is the original. What the model
gets is turned upright from its EXIF orientation, scaled down so its longer
side is at most ``image_max_px``, and encoded as JPEG, or PNG when it has
transparency. Pillow runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from alfred.attachments import Unreadable
from alfred.tools.base import ModelImage

# Larger images are refused, so a small file can't decode into gigabytes.
# Pillow itself raises at twice this.
MAX_PIXELS = 64_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

MIME_BY_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",  # its first frame
}
JPEG_QUALITY = 85
# EXIF orientations that turn the image a quarter, swapping width and height.
_QUARTER_TURNS = {5, 6, 7, 8}
_ORIENTATION = 0x0112


@dataclass(frozen=True)
class ImageInfo:
    mime: str
    # Upright, as it is shown.
    width: int
    height: int


def inspect_image(data: bytes) -> ImageInfo:
    """The image's type and upright size; :class:`Unreadable` if it can't be used."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = MIME_BY_FORMAT.get(image.format or "")
            if mime is None:
                raise Unreadable(f"unsupported image format {image.format}")
            width, height = image.size
            if width * height > MAX_PIXELS:
                raise Unreadable(f"image too large ({width}x{height})")
            image.load()  # decodes everything: a truncated file fails here
            orientation = image.getexif().get(_ORIENTATION)
    except Unreadable:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise Unreadable(f"image can't be decoded: {type(exc).__name__}") from None
    if orientation in _QUARTER_TURNS:
        width, height = height, width
    return ImageInfo(mime, width, height)


def for_model(data: bytes, max_px: int) -> ModelImage:
    """The image upright, at most ``max_px`` on its longer side, as JPEG or PNG."""
    try:
        with Image.open(io.BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((max_px, max_px))
            return encode(image)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise Unreadable(f"image can't be decoded: {type(exc).__name__}") from None


def encode(image: Image.Image) -> ModelImage:
    """PNG when the image has see-through pixels, JPEG otherwise."""
    out = io.BytesIO()
    if _transparent(image):
        image.convert("RGBA").save(out, "PNG", optimize=True)
        return ModelImage(out.getvalue(), "image/png")
    image.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY)
    return ModelImage(out.getvalue(), "image/jpeg")


def _transparent(image: Image.Image) -> bool:
    if image.mode == "P" and "transparency" in image.info:
        image = image.convert("RGBA")
    if image.mode not in ("RGBA", "LA", "PA"):
        return False
    low, _ = image.getchannel("A").getextrema()
    return low < 255


async def inspect_image_async(data: bytes) -> ImageInfo:
    return await asyncio.to_thread(inspect_image, data)


async def for_model_async(data: bytes, max_px: int) -> ModelImage:
    return await asyncio.to_thread(for_model, data, max_px)
