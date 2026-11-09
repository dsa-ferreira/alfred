"""PDFs: text on arrival, pages rendered on demand (design D7).

pdfium is not thread-safe, so every pypdfium2 call holds one lock, and the
async functions run them on one dedicated worker thread.

On arrival a PDF that needs a password, can't be parsed or has no pages is
refused; otherwise every page's text is extracted for ``attachment_pages``.
A page "has text" when its stripped text is at least 16 characters, so a
stray page number on a scanned page doesn't count as a text layer.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from alfred.attachments import Unreadable
from alfred.attachments.handles import has_text
from alfred.attachments.images import encode
from alfred.tools.base import ModelImage

__all__ = ["MIME", "PdfText", "has_text", "read_pdf", "render_page"]

MIME = "application/pdf"

_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdfium")


@dataclass(frozen=True)
class PdfText:
    """Each page's text, page 1 first."""

    pages: list[str]

    @property
    def page_count(self) -> int:
        return len(self.pages)


def _open(data: bytes) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if exc.err_code == pdfium_c.FPDF_ERR_PASSWORD:
            raise Unreadable("the PDF is password-protected") from None
        raise Unreadable("the PDF can't be opened (damaged or not a PDF)") from None


def read_pdf(data: bytes) -> PdfText:
    """Every page's text; :class:`Unreadable` if the PDF can't be used."""
    with _lock:
        document = _open(data)
        try:
            count = len(document)
            if count == 0:
                raise Unreadable("the PDF has no pages")
            pages = []
            for index in range(count):
                page = document[index]
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_range()
                    finally:
                        textpage.close()
                finally:
                    page.close()
                pages.append(text.replace("\r\n", "\n").replace("\r", "\n").strip())
            return PdfText(pages)
        finally:
            document.close()


def render_page(data: bytes, page_number: int, max_px: int) -> ModelImage:
    """Page ``page_number`` (from 1) as an image whose longer side is ``max_px``."""
    with _lock:
        document = _open(data)
        try:
            if not 1 <= page_number <= len(document):
                raise ValueError(f"page {page_number} of {len(document)}")
            page = document[page_number - 1]
            try:
                width, height = page.get_size()  # in points
                bitmap = page.render(scale=max_px / max(width, height))
                try:
                    image = bitmap.to_pil()
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            document.close()
    return encode(image)


async def _run[T](function: Callable[..., T], *args: object) -> T:
    return await asyncio.get_running_loop().run_in_executor(_executor, function, *args)


async def read_pdf_async(data: bytes) -> PdfText:
    return await _run(read_pdf, data)


async def render_page_async(data: bytes, page_number: int, max_px: int) -> ModelImage:
    return await _run(render_page, data, page_number, max_px)
