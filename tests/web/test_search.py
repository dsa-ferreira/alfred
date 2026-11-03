"""SearXNG search and result shaping, against a reply recorded from ``oracle.lan``."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from alfred.web.search import FAILED_ENGINES, SearchUnavailable, SearxngClient, shape

FIXTURE = Path(__file__).parent / "fixtures" / "searxng_lisbon_weather.json"
SEARXNG = "http://oracle.lan"


def reply() -> dict[str, Any]:
    """The recorded reply for "lisbon weather": 30 results, an infobox, one failed engine."""
    return copy.deepcopy(json.loads(FIXTURE.read_text()))


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def client(http: httpx.AsyncClient, *, results: int = 5, max_chars: int = 4000) -> SearxngClient:
    return SearxngClient(http, SEARXNG, results=results, timeout_s=5, max_chars=max_chars)


# --- Web search through SearXNG ---------------------------------------------------


@respx.mock
async def test_shaped_results(http: httpx.AsyncClient) -> None:
    data = reply()
    route = respx.get(f"{SEARXNG}/search").respond(json=data)
    found = await client(http).search("lisbon weather")

    request = route.calls.last.request
    assert dict(request.url.params) == {"q": "lisbon weather", "format": "json"}
    first_five = [r["url"] for r in data["results"][:5]]
    assert found.urls == first_five
    for n, result in enumerate(data["results"][:5], 1):
        assert f"{n}. {result['title']}\n   {result['url']}\n   " in found.text
    assert data["results"][5]["url"] not in found.text
    # None of SearXNG's other fields.
    listing = found.text.replace(FAILED_ENGINES, "")
    for field in ("thumbnail", "gstatic", "google cse", "score", "parsed_url", "engines"):
        assert field not in listing
    assert len(found.text) <= 4000


@respx.mock
async def test_snippet_and_date(http: httpx.AsyncClient) -> None:
    data = reply()
    data["results"][0]["publishedDate"] = "2026-11-02T09:10:00"
    respx.get(f"{SEARXNG}/search").respond(json=data)
    found = await client(http).search("lisbon weather")
    snippet = " ".join(data["results"][0]["content"].split())
    assert f"   2026-11-02 {snippet}" in found.text


@respx.mock
async def test_recent_news_filters(http: httpx.AsyncClient) -> None:
    route = respx.get(f"{SEARXNG}/search").respond(json=reply())
    await client(http).search("portugal", time_range="week", category="news")
    params = dict(route.calls.last.request.url.params)
    assert params == {"q": "portugal", "format": "json", "time_range": "week", "categories": "news"}


@respx.mock
async def test_no_results(http: httpx.AsyncClient) -> None:
    respx.get(f"{SEARXNG}/search").respond(
        json={
            "query": "xyzzy",
            "results": [],
            "answers": [],
            "infoboxes": [],
            "unresponsive_engines": [],
        }
    )
    found = await client(http).search("xyzzy")
    assert found.text == 'No web results found for "xyzzy".'
    assert found.urls == []


def test_result_count_follows_config() -> None:
    found = shape(reply(), "q", limit=3, max_chars=4000)
    assert len(found.urls) == 3


def test_duplicates_dropped_before_counting() -> None:
    data = reply()
    first = data["results"][0]["url"]
    data["results"].insert(1, {**data["results"][0], "url": first + "#top", "title": "Dup"})
    found = shape(data, "q", limit=5, max_chars=4000)
    assert found.urls == [r["url"] for r in reply()["results"][:5]]
    assert "Dup" not in found.text


def test_results_without_http_url_skipped() -> None:
    data = reply()
    data["results"][0]["url"] = "magnet:?xt=urn:btih:abc"
    found = shape(data, "q", limit=5, max_chars=4000)
    assert "magnet:" not in found.text
    assert len(found.urls) == 5


# --- Search extras ------------------------------------------------------------------


def test_direct_answer_first() -> None:
    data = reply()
    data["answers"] = [
        {
            "url": None,
            "engine": "currency",
            "template": "answer/legacy.html",
            "answer": "100 EUR = 108.12 USD",
        }
    ]
    found = shape(data, "100 eur to usd", limit=5, max_chars=4000)
    assert found.text.startswith("Answer: 100 EUR = 108.12 USD\n")


def test_plain_string_answer() -> None:
    data = reply()
    data["answers"] = ["42"]
    assert shape(data, "q", limit=5, max_chars=4000).text.startswith("Answer: 42\n")


def test_infobox_before_results() -> None:
    found = shape(reply(), "lisbon weather", limit=5, max_chars=4000)
    lines = found.text.splitlines()
    assert lines[0] == "Infobox: Lisbon weather: Wikimedia template"
    assert lines[1].startswith("1. ")


def test_long_infobox_shortened() -> None:
    data = reply()
    data["infoboxes"][0]["content"] = "word " * 1000
    line = shape(data, "q", limit=5, max_chars=4000).text.splitlines()[0]
    assert len(line) <= len("Infobox: ") + 500


def test_failed_engines_after_results() -> None:
    data = reply()
    data["unresponsive_engines"] = [["duckduckgo", "CAPTCHA"], ["google", "timeout"]]
    found = shape(data, "q", limit=5, max_chars=4000)
    assert found.text.endswith("\n" + FAILED_ENGINES)
    assert "CAPTCHA" not in found.text


def test_no_failed_engines_line_when_all_answered() -> None:
    data = reply()
    data["unresponsive_engines"] = []
    assert FAILED_ENGINES not in shape(data, "q", limit=5, max_chars=4000).text


def test_long_snippets_shortened_to_fit() -> None:
    data = reply()
    for result in data["results"]:
        result["content"] = "lorem ipsum " * 200
    found = shape(data, "q", limit=5, max_chars=1500)
    assert len(found.text) <= 1500
    assert len(found.urls) == 5
    assert all(url in found.text for url in found.urls)
    assert "…" in found.text


def test_tiny_limit_never_lists_hidden_urls() -> None:
    found = shape(reply(), "q", limit=5, max_chars=400)
    assert len(found.text) <= 400
    assert found.urls and all(url in found.text for url in found.urls)


# --- Search failures go to the model ----------------------------------------------


@respx.mock
async def test_searxng_down(http: httpx.AsyncClient) -> None:
    respx.get(f"{SEARXNG}/search").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(SearchUnavailable, match="can't reach the search server"):
        await client(http).search("q")


@respx.mock
async def test_searxng_timeout(http: httpx.AsyncClient) -> None:
    respx.get(f"{SEARXNG}/search").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(SearchUnavailable, match="timed out"):
        await client(http).search("q")


@respx.mock
async def test_searxng_error_status(http: httpx.AsyncClient) -> None:
    respx.get(f"{SEARXNG}/search").respond(403, text="format not allowed")
    with pytest.raises(SearchUnavailable, match="HTTP 403"):
        await client(http).search("q")


@respx.mock
async def test_searxng_not_json(http: httpx.AsyncClient) -> None:
    respx.get(f"{SEARXNG}/search").respond(200, text="<html>")
    with pytest.raises(SearchUnavailable, match="not JSON"):
        await client(http).search("q")
