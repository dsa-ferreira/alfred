"""PDF text on arrival, and page rendering (design D7)."""

from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

from alfred.attachments import Unreadable
from alfred.attachments.pdf import (
    has_text,
    read_pdf,
    read_pdf_async,
    render_page,
    render_page_async,
)
from tests.attachments.files import scanned_pdf, text_pdf

INVOICE_P1 = "Invoice 2026-118 from Lisbon Plumbing, total 240.00 EUR"


def test_text_pdf() -> None:
    pdf = read_pdf(text_pdf([INVOICE_P1, "Terms: payable within 30 days.", ""]))
    assert pdf.page_count == 3
    assert pdf.pages == [INVOICE_P1, "Terms: payable within 30 days.", ""]
    assert [has_text(p) for p in pdf.pages] == [True, True, False]


def test_scanned_pdf_has_pages_without_text() -> None:
    pdf = read_pdf(scanned_pdf(2))
    assert pdf.page_count == 2
    assert not any(has_text(p) for p in pdf.pages)


def test_stray_page_number_is_not_a_text_layer() -> None:
    assert not has_text("  12  ")
    assert not has_text("Page 3 of 12")  # 12 characters
    assert has_text("Page 3 of 12, continued")


def test_password_protected_pdf_refused() -> None:
    with pytest.raises(Unreadable, match="password"):
        read_pdf(text_pdf([INVOICE_P1], encrypt_password="hunter2"))


def test_damaged_pdf_refused() -> None:
    data = text_pdf([INVOICE_P1])
    with pytest.raises(Unreadable, match="can't be opened"):
        read_pdf(b"%PDF-1.4\n" + b"\x00" * 200)
    with pytest.raises(Unreadable):
        read_pdf(data[:40])


def test_pdf_without_pages_refused() -> None:
    # pdfium won't load a document with no pages at all (even one it saved).
    with pytest.raises(Unreadable):
        read_pdf(text_pdf([]))


def test_render_page_two_size() -> None:
    # Page 2 is 300x600 points, portrait: rendered 768x1536 for a 1536 limit.
    data = text_pdf([INVOICE_P1, "second"], sizes=[(612, 792), (300, 600)])
    image = render_page(data, 2, 1536)
    assert image.mime == "image/jpeg"
    assert Image.open(io.BytesIO(image.data)).size == (768, 1536)


def test_render_page_out_of_range() -> None:
    with pytest.raises(ValueError, match="page 3 of 2"):
        render_page(text_pdf(["a", "b"]), 3, 1536)


async def test_async_calls_share_one_thread() -> None:
    data = text_pdf([INVOICE_P1, "second page text, long enough"])
    read, image, *_ = await asyncio.gather(
        read_pdf_async(data),
        render_page_async(data, 1, 200),
        *(render_page_async(data, 2, 100) for _ in range(5)),
    )
    assert read.page_count == 2
    assert max(Image.open(io.BytesIO(image.data)).size) == 200
