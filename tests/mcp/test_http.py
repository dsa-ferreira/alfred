"""The production connector over Streamable HTTP: bearer token per server."""

from __future__ import annotations

import httpx2
from pydantic import SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

from alfred.config import McpServer
from alfred.mcp.client import http_connector
from alfred.mcp.registry import build_turn_tools, call_tool
from tests.mcp.servers import make_cookbook, make_mcparr


class RequireToken:
    """Rejects requests without the expected bearer token and records what it saw."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app, self.token, self.seen = app, token, []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            self.seen.append(auth)
            if auth != f"Bearer {self.token}":
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


class ByHost:
    def __init__(self, apps: dict[str, ASGIApp]) -> None:
        self.apps = apps

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            return
        port = dict(scope["headers"])[b"host"].decode().split(":")[1]
        await self.apps[port](scope, receive, send)


async def test_each_server_gets_its_own_token() -> None:
    mcparr, cookbook = make_mcparr(), make_cookbook()
    guard_a = RequireToken(mcparr.streamable_http_app(), "tok-a")
    guard_b = RequireToken(cookbook.streamable_http_app(), "tok-b")
    transport = httpx2.ASGITransport(app=ByHost({"1001": guard_a, "1002": guard_b}))
    connect = http_connector(transport)
    servers = [
        McpServer("mcparr", "http://localhost:1001/mcp", SecretStr("tok-a")),
        McpServer("cookbook", "http://localhost:1002/mcp", SecretStr("tok-b")),
        McpServer("wrong", "http://localhost:1001/mcp", SecretStr("tok-x")),
    ]
    async with mcparr.session_manager.run(), cookbook.session_manager.run():
        turn = await build_turn_tools(servers, connect, 5)
        assert turn.unavailable == ["wrong"]
        assert {"mcparr_get_downloads", "cookbook_get_recipe"} <= set(turn.routes)
        outcome = await call_tool(
            turn, "mcparr_add_media", '{"title": "Dune"}', connect=connect, call_timeout_s=5,
            max_chars=999,
        )  # fmt: skip
    assert outcome.ok and "added Dune" in outcome.text
    assert set(guard_a.seen) == {"Bearer tok-a", "Bearer tok-x"}
    assert set(guard_b.seen) == {"Bearer tok-b"}
