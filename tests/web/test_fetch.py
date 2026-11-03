"""Guarded fetch: address checks on every hop, redirects, limits and errors."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from alfred.web.fetch import (
    MAX_REDIRECTS,
    BlockedAddress,
    Fetcher,
    FetchError,
    UnsupportedType,
    is_public,
    make_http_client,
)

PUBLIC = "93.184.215.14"
HTML = {"content-type": "text/html; charset=utf-8"}

# host -> addresses; anything else fails to resolve.
DNS: dict[str, list[str]] = {
    "example.com": [PUBLIC],
    "www.example.com": [PUBLIC],
    "other.example": ["2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
    "cookbook.lan": ["192.168.1.20"],
    "mixed.example": [PUBLIC, "10.0.0.5"],
    "mapped.example": ["::ffff:192.168.1.1"],
    "192.168.1.10": ["192.168.1.10"],
}


class Resolver:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def __call__(self, host: str, port: int) -> list[str]:
        self.asked.append(host)
        if host not in DNS:
            raise OSError("Name or service not known")
        return DNS[host]


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with make_http_client() as client:
        yield client


def fetcher(
    http: httpx.AsyncClient,
    resolver: Resolver | None = None,
    *,
    timeout_s: float = 5,
    max_bytes: int = 5_000_000,
) -> Fetcher:
    return Fetcher(http, timeout_s=timeout_s, max_bytes=max_bytes, resolver=resolver or Resolver())


@pytest.mark.parametrize(
    ("address", "public"),
    [
        (PUBLIC, True),
        ("2606:4700::1111", True),
        ("192.168.1.10", False),
        ("10.1.2.3", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("fe80::1%eth0", False),
        ("fd00::1", False),
        ("::ffff:192.168.1.1", False),
        ("::ffff:8.8.8.8", True),
        ("not an ip", False),
    ],
)
def test_is_public(address: str, public: bool) -> None:
    assert is_public(address) is public


@respx.mock
async def test_page_downloaded(http: httpx.AsyncClient) -> None:
    route = respx.get("https://example.com/a").respond(200, headers=HTML, content=b"<p>hi</p>")
    page = await fetcher(http).fetch("https://example.com/a")
    assert (page.url, page.status, page.content_type, page.charset) == (
        "https://example.com/a",
        200,
        "text/html",
        "utf-8",
    )
    assert page.body == b"<p>hi</p>"
    assert route.calls.last.request.headers["user-agent"].startswith("alfred")


# --- Private addresses blocked ----------------------------------------------------


@respx.mock
async def test_lan_host_in_a_search_result(http: httpx.AsyncClient) -> None:
    route = respx.get("http://cookbook.lan:8080/").respond(200, headers=HTML, text="secret")
    with pytest.raises(BlockedAddress, match="private or local address"):
        await fetcher(http).fetch("http://cookbook.lan:8080/")
    assert not route.called


@pytest.mark.parametrize("host", ["mixed.example", "mapped.example"])
@respx.mock
async def test_any_private_address_blocks(http: httpx.AsyncClient, host: str) -> None:
    route = respx.get(f"https://{host}/").respond(200, headers=HTML, text="x")
    with pytest.raises(BlockedAddress):
        await fetcher(http).fetch(f"https://{host}/")
    assert not route.called


@respx.mock
async def test_redirect_to_a_private_address(http: httpx.AsyncClient) -> None:
    respx.get("https://example.com/go").respond(302, headers={"location": "http://192.168.1.10/"})
    inside = respx.get("http://192.168.1.10/").respond(200, headers=HTML, text="router")
    resolver = Resolver()
    with pytest.raises(BlockedAddress, match="redirected to http://192.168.1.10/.*not followed"):
        await fetcher(http, resolver).fetch("https://example.com/go")
    assert not inside.called
    assert resolver.asked == ["example.com", "192.168.1.10"]


@respx.mock
async def test_ordinary_redirect(http: httpx.AsyncClient) -> None:
    respx.get("http://example.com/a").respond(301, headers={"location": "https://example.com/a"})
    respx.get("https://example.com/a").respond(200, headers=HTML, text="<p>page</p>")
    page = await fetcher(http).fetch("http://example.com/a")
    assert page.url == "https://example.com/a"
    assert page.body == b"<p>page</p>"


@respx.mock
async def test_relative_redirect(http: httpx.AsyncClient) -> None:
    respx.get("https://example.com/old").respond(308, headers={"location": "/new"})
    respx.get("https://example.com/new").respond(200, headers=HTML, text="new")
    assert (await fetcher(http).fetch("https://example.com/old")).url == "https://example.com/new"


@respx.mock
async def test_redirect_to_other_scheme(http: httpx.AsyncClient) -> None:
    respx.get("https://example.com/a").respond(302, headers={"location": "file:///etc/passwd"})
    with pytest.raises(FetchError, match="not an http"):
        await fetcher(http).fetch("https://example.com/a")


@respx.mock
async def test_too_many_redirects(http: httpx.AsyncClient) -> None:
    for n in range(MAX_REDIRECTS + 1):
        respx.get(f"https://example.com/{n}").respond(
            302, headers={"location": f"https://example.com/{n + 1}"}
        )
    respx.get(f"https://example.com/{MAX_REDIRECTS + 1}").respond(200, headers=HTML, text="x")
    with pytest.raises(FetchError, match="too many redirects"):
        await fetcher(http).fetch("https://example.com/0")


@respx.mock
async def test_unknown_host(http: httpx.AsyncClient) -> None:
    with pytest.raises(FetchError, match="could not be resolved"):
        await fetcher(http).fetch("https://nowhere.invalid/")


# --- Fetch limits -----------------------------------------------------------------


@respx.mock
async def test_huge_download(http: httpx.AsyncClient) -> None:
    sent = 0

    async def fifty_megabytes() -> AsyncIterator[bytes]:
        nonlocal sent
        for _ in range(50):
            sent += 1
            yield b"x" * 1_000_000

    respx.get("https://example.com/big").mock(
        return_value=httpx.Response(200, headers=HTML, content=fifty_megabytes())
    )
    with pytest.raises(FetchError, match="too large"):
        await fetcher(http).fetch("https://example.com/big")
    assert sent <= 6  # stopped at the 5 MB cap


@respx.mock
async def test_declared_length_over_the_cap(http: httpx.AsyncClient) -> None:
    respx.get("https://example.com/big").respond(
        200, headers={**HTML, "content-length": "50000000"}, content=b"x"
    )
    with pytest.raises(FetchError, match="too large"):
        await fetcher(http).fetch("https://example.com/big")


@respx.mock
async def test_slow_host(http: httpx.AsyncClient) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, headers=HTML, text="late")

    respx.get("https://example.com/slow").mock(side_effect=slow)
    with pytest.raises(FetchError, match="timed out"):
        await fetcher(http, timeout_s=0.1).fetch("https://example.com/slow")


@respx.mock
async def test_timeout_covers_every_hop(http: httpx.AsyncClient) -> None:
    async def slow_redirect(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.08)
        return httpx.Response(302, headers={"location": "https://example.com/next"})

    respx.get("https://example.com/first").mock(side_effect=slow_redirect)
    respx.get("https://example.com/next").mock(side_effect=slow_redirect)
    with pytest.raises(FetchError, match="timed out"):
        await fetcher(http, timeout_s=0.12).fetch("https://example.com/first")


@respx.mock
async def test_transport_timeout(http: httpx.AsyncClient) -> None:
    respx.get("https://example.com/a").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FetchError, match="timed out"):
        await fetcher(http).fetch("https://example.com/a")


# --- Fetch failures -----------------------------------------------------------------


@respx.mock
async def test_not_found(http: httpx.AsyncClient) -> None:
    respx.get("https://example.com/missing").respond(404, headers=HTML, text="nope")
    with pytest.raises(FetchError, match="the page returned HTTP 404"):
        await fetcher(http).fetch("https://example.com/missing")


@respx.mock
async def test_network_error(http: httpx.AsyncClient) -> None:
    respx.get("https://example.com/a").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(FetchError, match="could not be reached"):
        await fetcher(http).fetch("https://example.com/a")


@respx.mock
async def test_type_refused_before_reading(http: httpx.AsyncClient) -> None:
    read = False

    async def image() -> AsyncIterator[bytes]:
        nonlocal read
        read = True
        yield b"\x89PNG"

    respx.get("https://example.com/i.png").mock(
        return_value=httpx.Response(200, headers={"content-type": "image/png"}, content=image())
    )
    with pytest.raises(UnsupportedType, match="image/png is not supported"):
        await fetcher(http).fetch("https://example.com/i.png", accept=lambda t: t == "text/html")
    assert not read


# --- No cookies ---------------------------------------------------------------------


@respx.mock
async def test_no_cookies_kept(http: httpx.AsyncClient) -> None:
    respx.get("https://example.com/a").respond(
        200, headers={**HTML, "set-cookie": "session=abc; Path=/"}, text="a"
    )
    second = respx.get("https://example.com/b").respond(200, headers=HTML, text="b")
    f = fetcher(http)
    await f.fetch("https://example.com/a")
    await f.fetch("https://example.com/b")
    assert "cookie" not in second.calls.last.request.headers
    assert len(http.cookies.jar) == 0
