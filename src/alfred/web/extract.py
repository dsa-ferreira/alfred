"""Page text from a download (change add-web-search, design D6).

HTML goes through trafilatura, which keeps the main content (dropping menus,
footers, cookie banners and comments) as light Markdown with headings,
lists, links and tables. Plain text is used as is. Nothing else is read, and
no JavaScript runs: a page built in the browser has no readable content.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import trafilatura
from trafilatura.utils import decode_file

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
TEXT_TYPES = frozenset({"text/plain"})


class NoReadableContent(Exception):
    """The page has no text alfred can extract."""


@dataclass(frozen=True)
class PageText:
    title: str | None
    text: str


def supported(content_type: str) -> bool:
    """Whether a media type (``text/html``, without parameters) can be read."""
    return content_type in HTML_TYPES or content_type in TEXT_TYPES


async def extract(
    body: bytes, content_type: str, charset: str | None = None, url: str | None = None
) -> PageText:
    """The readable text of a page. Raises :class:`NoReadableContent`.

    ``content_type`` must be :func:`supported`. trafilatura is CPU-bound, so
    HTML runs in a worker thread.
    """
    if content_type in TEXT_TYPES:
        page = PageText(None, _decode(body, charset).strip())
    elif content_type in HTML_TYPES:
        page = await asyncio.to_thread(_extract_html, body, charset, url)
    else:
        raise ValueError(f"unsupported content type {content_type!r}")
    if not page.text:
        raise NoReadableContent
    return page


def _extract_html(body: bytes, charset: str | None, url: str | None) -> PageText:
    # Without a charset header, trafilatura guesses it (meta tag, detection).
    html = _decode(body, charset) if charset else decode_file(body)
    # No url= here: trafilatura would join relative links to the site root
    # (``changelog.html`` -> ``https://host/changelog.html``), which is wrong
    # for pages below it. Links are kept as the page wrote them.
    text = trafilatura.extract(
        html,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        include_comments=False,
    )
    metadata = trafilatura.extract_metadata(html, default_url=url)
    title = metadata.title.strip() if metadata is not None and metadata.title else None
    return PageText(title or None, (text or "").strip())


def _decode(body: bytes, charset: str | None) -> str:
    if charset:
        try:
            return body.decode(charset, errors="replace")
        except LookupError:
            pass
    return body.decode("utf-8", errors="replace")
