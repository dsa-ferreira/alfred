"""Web search through SearXNG's JSON API (change add-web-search, design D4).

SearXNG returns about 20 fields per result and often 30 results. The model
gets a short plain-text list instead: direct answers and infoboxes first,
then the top results (title, URL, date when known, snippet), then a line if
some engines failed. Snippets are shortened so the whole text fits
``[ai] tool_result_max_chars``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from alfred.web.allowlist import normalise

log = logging.getLogger(__name__)

TimeRange = Literal["day", "week", "month", "year"]
Category = Literal["general", "news"]
TIME_RANGES: tuple[str, ...] = ("day", "week", "month", "year")
CATEGORIES: tuple[str, ...] = ("general", "news")

SNIPPET_CHARS = 400
INFOBOX_CHARS = 500
ANSWER_CHARS = 500
MIN_SNIPPET_CHARS = 40
FAILED_ENGINES = "(Some search engines did not respond.)"


class SearchUnavailable(Exception):
    """SearXNG could not answer. The message is safe to show the model."""


@dataclass(frozen=True)
class SearchResults:
    text: str
    # The URLs listed in ``text``, as SearXNG gave them.
    urls: list[str]


@dataclass(frozen=True)
class _Item:
    title: str
    url: str
    date: str | None
    snippet: str


class SearxngClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        *,
        results: int,
        timeout_s: float,
        max_chars: int,
    ) -> None:
        self._http = http
        self._url = f"{base_url.rstrip('/')}/search"
        self._results = results
        self._timeout = httpx.Timeout(timeout_s)
        self._max_chars = max_chars

    async def search(
        self, query: str, *, time_range: str | None = None, category: str | None = None
    ) -> SearchResults:
        params = {"q": query, "format": "json"}
        if time_range:
            params["time_range"] = time_range
        if category:
            params["categories"] = category
        try:
            response = await self._http.get(self._url, params=params, timeout=self._timeout)
        except httpx.TimeoutException:
            raise SearchUnavailable("the search server timed out") from None
        except httpx.HTTPError as exc:
            reason = f"can't reach the search server ({type(exc).__name__})"
            raise SearchUnavailable(reason) from None
        if response.status_code != 200:
            raise SearchUnavailable(f"the search server answered HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise SearchUnavailable("the search server's reply is not JSON") from None
        if not isinstance(data, dict):
            raise SearchUnavailable("the search server's reply can't be understood")
        return shape(data, query, limit=self._results, max_chars=self._max_chars)


def shape(data: dict[str, Any], query: str, *, limit: int, max_chars: int) -> SearchResults:
    """SearXNG's JSON reply as the text the model reads, and the URLs it lists."""
    head: list[str] = []
    for answer in _list(data.get("answers"))[:1]:
        text = answer.get("answer") if isinstance(answer, dict) else answer
        if isinstance(text, str) and text.strip():
            head.append(f"Answer: {_cut(_flat(text), ANSWER_CHARS)}")
    for box in _list(data.get("infoboxes"))[:1]:
        if not isinstance(box, dict):
            continue
        title = _flat(_str(box.get("infobox")) or _str(box.get("title")))
        content = _flat(_str(box.get("content")))
        if title or content:
            line = f"{title}: {content}" if title and content else title or content
            head.append(f"Infobox: {_cut(line, INFOBOX_CHARS)}")

    items = _items(data.get("results"), limit)
    tail = [FAILED_ENGINES] if _list(data.get("unresponsive_engines")) else []

    if not items:
        body = [f'No web results found for "{query}".']
        return SearchResults(_fit("\n".join(head + body + tail), max_chars), [])

    # Snippets share what is left of max_chars after everything else. If even
    # titles and URLs don't fit, the last results go, so every URL returned
    # (and allowed for fetch) is one the model can see.
    while True:
        fixed = "\n".join(head + [_render(n, item, "") for n, item in enumerate(items, 1)] + tail)
        if len(fixed) <= max_chars or len(items) == 1:
            break
        items.pop()
    room = (max_chars - len(fixed)) // len(items)
    budget = min(SNIPPET_CHARS, room)
    if budget < MIN_SNIPPET_CHARS:  # too little room for a useful snippet
        budget = 0
    lines = [
        _render(n, item, _cut(item.snippet, budget) if budget else "")
        for n, item in enumerate(items, 1)
    ]
    text = "\n".join(head + lines + tail)
    return SearchResults(_fit(text, max_chars), [item.url for item in items])


def _items(results: Any, limit: int) -> list[_Item]:
    seen: set[str] = set()
    items: list[_Item] = []
    for result in _list(results):
        if len(items) >= limit:
            break
        if not isinstance(result, dict):
            continue
        url = _str(result.get("url")).strip()
        key = normalise(url)
        if key is None or key in seen:
            continue
        seen.add(key)
        date = _str(result.get("publishedDate"))[:10] or None
        items.append(
            _Item(
                title=_flat(_str(result.get("title"))) or url,
                url=url,
                date=date,
                snippet=_flat(_str(result.get("content"))),
            )
        )
    return items


def _render(n: int, item: _Item, snippet: str) -> str:
    line = f"{n}. {item.title}\n   {item.url}"
    detail = " ".join(part for part in (item.date, snippet) if part)
    return f"{line}\n   {detail}" if detail else line


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _flat(text: str) -> str:
    return " ".join(text.split())


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _fit(text: str, max_chars: int) -> str:
    """Last resort when titles and URLs alone exceed ``max_chars``."""
    return text if len(text) <= max_chars else _cut(text, max_chars)
