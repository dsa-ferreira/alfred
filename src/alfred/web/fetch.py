"""Guarded page download (change add-web-search, design D5).

```
for each hop (the URL, then up to MAX_REDIRECTS redirects):
    scheme must be http/https
    resolve the host; any address not globally routable -> refused
    GET without following redirects; 3xx -> next hop with Location
error status -> error; unsupported type -> error (before the body is read)
read the body in chunks, stop past max_bytes -> "too large"
```

One total timeout covers every hop. The shared client keeps no cookies
(:func:`make_http_client`). Checking the address before connecting leaves a
DNS-rebinding window, accepted in the design: fetch targets are already
limited by the allowlist.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger(__name__)

MAX_REDIRECTS = 5


def _user_agent() -> str:
    try:
        return f"alfred/{version('alfred')}"
    except PackageNotFoundError:
        return "alfred"


USER_AGENT = _user_agent()
_REDIRECTS = {301, 302, 303, 307, 308}

Resolver = Callable[[str, int], Awaitable[list[str]]]


class FetchError(Exception):
    """A fetch that produced no page. The message is safe to show the model."""


class BlockedAddress(FetchError):
    pass


class UnsupportedType(FetchError):
    def __init__(self, content_type: str) -> None:
        super().__init__(f"the content type {content_type or 'unknown'} is not supported")
        self.content_type = content_type


@dataclass(frozen=True)
class Download:
    url: str  # the final URL, after redirects
    status: int
    content_type: str  # media type only, lower case, e.g. "text/html"
    charset: str | None
    body: bytes


def make_http_client() -> httpx.AsyncClient:
    """The client web search and fetch share: no cookies kept, alfred's User-Agent."""
    jar = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
    return httpx.AsyncClient(
        cookies=jar, headers={"User-Agent": USER_AGENT}, follow_redirects=False
    )


async def resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def is_public(address: str) -> bool:
    """Whether ``address`` is globally routable (IPv4-mapped IPv6 judged as IPv4)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class Fetcher:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        timeout_s: float,
        max_bytes: int,
        resolver: Resolver = resolve,
    ) -> None:
        self._http = http
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes
        self._resolve = resolver

    async def fetch(self, url: str, accept: Callable[[str], bool] | None = None) -> Download:
        """Download ``url``. ``accept`` decides on the media type before the body is read.

        Raises :class:`FetchError` (or a subclass) with a message for the model.
        """
        try:
            async with asyncio.timeout(self._timeout_s):
                download = await self._fetch(url, accept)
        except TimeoutError:
            log.info("fetch %s refused: timed out after %ss", url, self._timeout_s)
            raise FetchError(f"the fetch timed out after {self._timeout_s:g} seconds") from None
        except FetchError as exc:
            log.info("fetch %s refused: %s", url, exc)
            raise
        log.info(
            "fetch %s: HTTP %d, %s, %d bytes",
            download.url,
            download.status,
            download.content_type,
            len(download.body),
        )
        return download

    async def _fetch(self, url: str, accept: Callable[[str], bool] | None) -> Download:
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            await self._check(current, redirected=hop > 0)
            request = self._http.build_request(
                "GET", current, timeout=httpx.Timeout(self._timeout_s)
            )
            try:
                response = await self._http.send(request, stream=True, follow_redirects=False)
            except httpx.TimeoutException:
                raise TimeoutError from None
            except httpx.HTTPError as exc:
                raise FetchError(f"the page could not be reached ({type(exc).__name__})") from None
            try:
                self._http.cookies.clear()  # belt and braces: the jar refuses cookies anyway
                if response.status_code in _REDIRECTS:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError(
                            f"the page answered HTTP {response.status_code} without a location"
                        )
                    current = urljoin(current, location)
                    continue
                return await self._read(current, response, accept)
            finally:
                await response.aclose()
        raise FetchError(f"too many redirects (more than {MAX_REDIRECTS})")

    async def _check(self, url: str, *, redirected: bool) -> None:
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            raise FetchError(f"invalid URL {url}") from None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            what = "redirected to" if redirected else "the URL"
            raise FetchError(f"{what} {url}, which is not an http(s) URL")
        host = parts.hostname
        try:
            addresses = await self._resolve(host, port or (443 if parts.scheme == "https" else 80))
        except OSError:
            raise FetchError(f"the host {host} could not be resolved") from None
        if not addresses or not all(is_public(a) for a in addresses):
            if redirected:
                raise BlockedAddress(
                    f"the page redirected to {url}, whose host resolves to a private or local "
                    "address; the redirect was not followed"
                )
            raise BlockedAddress(
                f"the host {host} resolves to a private or local address, "
                "which alfred never fetches"
            )

    async def _read(
        self, url: str, response: httpx.Response, accept: Callable[[str], bool] | None
    ) -> Download:
        status = response.status_code
        if status >= 400:
            raise FetchError(f"the page returned HTTP {status}")
        if status != 200:
            raise FetchError(f"the page answered HTTP {status}, which alfred can't read")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if accept is not None and not accept(content_type):
            raise UnsupportedType(content_type)
        too_large = FetchError(f"the page is too large (over {self._max_bytes:,} bytes)")
        length = response.headers.get("content-length", "")
        if length.isdigit() and int(length) > self._max_bytes:
            raise too_large
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > self._max_bytes:
                    raise too_large
        except httpx.TimeoutException:
            raise TimeoutError from None
        except httpx.HTTPError as exc:
            raise FetchError(f"the download failed ({type(exc).__name__})") from None
        return Download(url, status, content_type, response.charset_encoding, bytes(body))
