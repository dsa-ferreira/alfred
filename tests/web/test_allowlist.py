from __future__ import annotations

import pytest

from alfred.web.allowlist import Allowlist, find_urls, normalise


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Example.COM/Page?q=A", "https://example.com/Page?q=A"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/page#section-2", "https://example.com/page"),
        ("HTTPS://example.com/a", "https://example.com/a"),
    ],
)
def test_normalise(raw: str, expected: str) -> None:
    assert normalise(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["ftp://example.com/file", "file:///etc/passwd", "example.com", "https://", "http://x:99999/"],
)
def test_normalise_rejects(raw: str) -> None:
    assert normalise(raw) is None


def test_fragment_ignored() -> None:
    allowed = Allowlist()
    allowed.add("https://example.com/page")
    assert allowed.allows("https://example.com/page#section-2")


def test_other_scheme_refused() -> None:
    allowed = Allowlist(["get ftp://example.com/file please"])
    assert not allowed.allows("ftp://example.com/file")
    assert len(allowed) == 0


def test_path_and_query_must_match() -> None:
    allowed = Allowlist(["https://example.com/a?x=1"])
    assert allowed.allows("https://EXAMPLE.com/a?x=1")
    assert not allowed.allows("https://example.com/a?x=2")
    assert not allowed.allows("https://example.com/b")


def test_extraction_strips_trailing_punctuation() -> None:
    text = (
        "summarise https://example.com/article. Also (see https://example.org/x), "
        'and "https://example.net/q?a=1"! or <https://example.io/y>; last: http://a.example/b?'
    )
    assert find_urls(text) == [
        "https://example.com/article",
        "https://example.org/x",
        "https://example.net/q?a=1",
        "https://example.io/y",
        "http://a.example/b",
    ]


def test_allowlist_from_pasted_text() -> None:
    allowed = Allowlist(["summarise https://example.com/article."])
    assert allowed.allows("https://example.com/article")
    assert not allowed.allows("https://example.com/other")
