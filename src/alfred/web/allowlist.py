"""Which URLs a turn may fetch (change add-web-search, design D3).

A URL is allowed when it appeared in an owner message in the turn's context,
in the ``/ask`` prompt, or in a search result earlier in the same turn.
URLs are compared in a normal form: ``http``/``https`` only, host in lower
case, default port and fragment removed, path and query kept as written.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}>'\""
_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalise(url: str) -> str | None:
    """The comparable form of ``url``, or ``None`` if it is not an http(s) URL with a host."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = host if port is None or port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"
    if parts.username or parts.password:
        userinfo = parts.username or ""
        if parts.password:
            userinfo += f":{parts.password}"
        netloc = f"{userinfo}@{netloc}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def find_urls(text: str) -> list[str]:
    """http(s) URLs written in ``text``, with trailing punctuation stripped."""
    return [match.group().rstrip(_TRAILING) for match in _URL.finditer(text)]


class Allowlist:
    """The normalised URLs one turn may fetch."""

    def __init__(self, texts: Iterable[str] = ()) -> None:
        self._urls: set[str] = set()
        for text in texts:
            self.add_from_text(text)

    def add(self, url: str) -> None:
        normal = normalise(url)
        if normal is not None:
            self._urls.add(normal)

    def add_from_text(self, text: str) -> None:
        for url in find_urls(text):
            self.add(url)

    def allows(self, url: str) -> bool:
        normal = normalise(url)
        return normal is not None and normal in self._urls

    def __len__(self) -> int:
        return len(self._urls)
