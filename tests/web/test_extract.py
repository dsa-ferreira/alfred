"""Page text extraction with trafilatura."""

from __future__ import annotations

import pytest

from alfred.web.extract import NoReadableContent, extract, supported

ARTICLE = """\
<!doctype html>
<html><head>
<title>Tram 28 returns to Graça | Lisbon Daily</title>
<meta property="og:title" content="Tram 28 returns to Graça">
</head><body>
<header><nav><a href="/">Home</a> <a href="/politics">Politics</a> <a href="/sport">Sport</a>
<a href="/subscribe">Subscribe now</a></nav></header>
<div class="cookie-banner" id="cookie-consent">We use cookies to improve your experience.
<button>Accept all cookies</button></div>
<main><article>
<h1>Tram 28 returns to Graça</h1>
<p>After eight months of track works, the historic tram 28 is running again between
Martim Moniz and Campo de Ourique, the city's transport company said on Monday.</p>
<h2>What changes for riders</h2>
<ul><li>Trams every 9 minutes at peak hours</li><li>New stop at Largo da Graça</li></ul>
<p>The full timetable is on the <a href="https://www.carris.pt/horarios">Carris website</a>.
Single tickets still cost 3.10 euros on board.</p>
</article></main>
<footer><p>© 2026 Lisbon Daily. All rights reserved.</p>
<a href="/privacy">Privacy policy</a></footer>
</body></html>
"""

SPA = """\
<!doctype html>
<html><head><title>Dashboard</title></head>
<body><div id="root"></div><script src="/static/js/main.js"></script></body></html>
"""


@pytest.mark.parametrize(
    ("content_type", "ok"),
    [
        ("text/html", True),
        ("application/xhtml+xml", True),
        ("text/plain", True),
        ("image/png", False),
        ("application/pdf", False),
        ("application/json", False),
        ("", False),
    ],
)
def test_supported_types(content_type: str, ok: bool) -> None:
    assert supported(content_type) is ok


async def test_article_page() -> None:
    page = await extract(ARTICLE.encode(), "text/html", "utf-8", "https://lisbondaily.example/t28")
    assert page.title == "Tram 28 returns to Graça"
    assert "historic tram 28 is running again" in page.text
    assert "## What changes for riders" in page.text
    assert "- Trams every 9 minutes at peak hours" in page.text
    assert "[Carris website](https://www.carris.pt/horarios)" in page.text
    furniture = ("Subscribe now", "Accept all cookies", "All rights reserved", "Privacy policy")
    for text in furniture:
        assert text not in page.text


async def test_charset_from_meta_tag() -> None:
    html = ARTICLE.replace("<head>", '<head><meta charset="iso-8859-1">', 1)
    page = await extract(html.encode("latin-1"), "text/html")
    assert page.title == "Tram 28 returns to Graça"
    assert "Largo da Graça" in page.text


async def test_plain_text_as_is() -> None:
    page = await extract(b"  line one\n\nline two  \n", "text/plain", "utf-8")
    assert page == type(page)(None, "line one\n\nline two")


async def test_page_built_by_javascript() -> None:
    with pytest.raises(NoReadableContent):
        await extract(SPA.encode(), "text/html", "utf-8")


async def test_empty_plain_text() -> None:
    with pytest.raises(NoReadableContent):
        await extract(b"   \n", "text/plain")


async def test_image_refused() -> None:
    with pytest.raises(ValueError, match="image/png"):
        await extract(b"\x89PNG", "image/png")


async def test_relative_links_kept_as_written() -> None:
    html = ARTICLE.replace(
        "</article>", '<p>See the <a href="changelog.html">changelog</a> for more.</p></article>'
    )
    page = await extract(html.encode(), "text/html", "utf-8", "https://docs.example/3/new.html")
    assert "[changelog](changelog.html)" in page.text
