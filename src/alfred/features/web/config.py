"""``[web]``: web search through SearXNG, and page fetch."""

from __future__ import annotations

from alfred.infra.settings import HttpUrlStr, PositiveFloat, PositiveInt, Section


class WebConfig(Section):
    """Web search through SearXNG and page fetch."""

    searxng_url: HttpUrlStr = "http://oracle.lan"
    search_results: PositiveInt = 5
    fetch_page_chars: PositiveInt = 8000
    fetch_timeout_s: PositiveFloat = 20
    fetch_max_bytes: PositiveInt = 5_000_000
