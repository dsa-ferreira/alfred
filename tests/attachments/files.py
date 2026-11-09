"""Small images and PDFs made in the tests."""

from __future__ import annotations

import hashlib
import io

from PIL import Image


def image_bytes(
    width: int = 40,
    height: int = 30,
    fmt: str = "JPEG",
    *,
    mode: str = "RGB",
    color: tuple[int, ...] = (200, 30, 30),
    orientation: int | None = None,
) -> bytes:
    image = Image.new(mode, (width, height), color)
    out = io.BytesIO()
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(out, fmt, exif=exif)
    else:
        image.save(out, fmt)
    return out.getvalue()


def scanned_pdf(pages: int = 1, size: tuple[int, int] = (600, 800)) -> bytes:
    """A PDF whose pages are only images (no text layer), as a scanner makes."""
    images = [Image.new("RGB", size, (250, 250, 245)) for _ in range(pages)]
    out = io.BytesIO()
    images[0].save(out, "PDF", save_all=True, append_images=images[1:], resolution=72)
    return out.getvalue()


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_pdf(
    pages: list[str],
    *,
    sizes: list[tuple[int, int]] | None = None,
    encrypt_password: str | None = None,
) -> bytes:
    """A PDF with one line of Helvetica text per page (empty text: a blank page).

    With ``encrypt_password``, the file uses the standard security handler
    (RC4, 40 bits) with that user password, so it can't be opened without it.
    """
    sizes = sizes or [(612, 792)] * len(pages)
    objects: list[bytes] = []
    kids = []
    font = 3 + 2 * len(pages)
    for i, (text, (w, h)) in enumerate(zip(pages, sizes, strict=True)):
        page_no, content_no = 3 + 2 * i, 4 + 2 * i
        kids.append(f"{page_no} 0 R")
        stream = f"BT /F1 12 Tf 72 {h - 72} Td ({_escape(text)}) Tj ET".encode() if text else b""
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content_no} 0 R >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    head = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode(),
    ]
    objects = head + objects + [b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    file_id = hashlib.md5(b"alfred-test").digest()
    trailer_extra = b""
    if encrypt_password is not None:
        objects.append(_encrypt_dict(encrypt_password, file_id))
        trailer_extra = b" /Encrypt %d 0 R" % len(objects)

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % n + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    hex_id = file_id.hex().encode()
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R /ID [<%s> <%s>]%s >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, hex_id, hex_id, trailer_extra, xref)
    )
    return out.getvalue()


_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    for byte in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def _encrypt_dict(user_password: str, file_id: bytes) -> bytes:
    """Standard security handler, revision 2 (PDF 1.7, algorithms 2, 3 and 4)."""
    padded_user = (user_password.encode("latin-1") + _PAD)[:32]
    owner = _rc4(hashlib.md5(padded_user).digest()[:5], padded_user)  # owner = user password
    permissions = -4
    key = hashlib.md5(
        padded_user + owner + permissions.to_bytes(4, "little", signed=True) + file_id
    ).digest()[:5]
    user = _rc4(key, _PAD)
    return b"<< /Filter /Standard /V 1 /R 2 /O <%s> /U <%s> /P %d >>" % (
        owner.hex().encode(),
        user.hex().encode(),
        permissions,
    )
