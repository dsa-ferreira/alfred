"""The production connector over Streamable HTTP: bearer token per server."""

from __future__ import annotations

from typing import Annotated

import httpx2
import pytest
from mcp.server import MCPServer
from mcp.server.elicitation import ElicitationResult
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.shared.exceptions import MCPError
from mcp.types import HEADER_MISMATCH
from pydantic import BaseModel, Field, SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

from alfred.features.mcp.client import http_connector
from alfred.features.mcp.config import McpServer
from alfred.features.mcp.registry import build_turn_tools, call_tool, resume_tool
from tests.features.mcp.servers import make_asking, make_cookbook, make_mcparr


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


# --- Server questions over Streamable HTTP (change add-user-input-requests) ---------


class Region(BaseModel):
    ok: bool


def ask_region(region: str) -> Elicit[Region]:
    return Elicit(f"Search in {region}?", Region)


def make_headered() -> MCPServer:
    """A server whose tool mirrors an argument in a header (``x-mcp-header``) and asks first."""
    server = MCPServer("mcparr")

    @server.tool()
    def search(
        region: Annotated[str, Field(json_schema_extra={"x-mcp-header": "Region"})],
        answer: Annotated[ElicitationResult[Region], Resolve(ask_region)],
    ) -> str:
        """Search one region."""
        return f"searched {region}: {answer.action}"

    @server.tool()
    def lookup(region: Annotated[str, Field(json_schema_extra={"x-mcp-header": "Region"})]) -> str:
        """Look up one region."""
        return f"looked up {region}"

    @server.tool()
    def ping() -> str:
        """No header arguments."""
        return "pong"

    return server


SERVER = McpServer("mcparr", "http://localhost:1001/mcp", SecretStr("tok"))


async def test_server_question_over_http() -> None:
    """The client declares form elicitation, so asking no longer fails the call."""
    mcparr = make_asking()
    connect = http_connector(httpx2.ASGITransport(app=mcparr.streamable_http_app()))
    async with mcparr.session_manager.run():
        turn = await build_turn_tools([SERVER], connect, 5)
        need = await call_tool(
            turn, "mcparr_add_media", '{"title": "Dune"}', connect=connect, call_timeout_s=5,
            max_chars=999,
        )  # fmt: skip
        assert need.need is not None, need.text
        yes = need.need.question.options[0].answer
        done = await resume_tool(
            turn, "mcparr_add_media", '{"title": "Dune"}', need.need.state, yes,
            connect=connect, call_timeout_s=5, max_chars=999,
        )  # fmt: skip
    assert done.ok and "added Dune" in done.text


async def test_fresh_connection_gets_header_mismatch_for_a_headered_tool() -> None:
    """The finding of task 4.4: a session-level call on a connection that hasn't
    listed a tool with ``x-mcp-header`` arguments is refused with HEADER_MISMATCH;
    a plain tool is not."""
    server = make_headered()
    connect = http_connector(httpx2.ASGITransport(app=server.streamable_http_app()))
    async with server.session_manager.run():
        async with connect(SERVER, 5) as client:
            with pytest.raises(MCPError) as refused:
                await client.session.call_tool("lookup", {"region": "eu"})
            assert refused.value.code == HEADER_MISMATCH
        async with connect(SERVER, 5) as client:
            plain = await client.session.call_tool("ping", {})
    assert not plain.is_error


async def test_headered_tool_is_relisted_and_resent() -> None:
    server = make_headered()
    connect = http_connector(httpx2.ASGITransport(app=server.streamable_http_app()))
    async with server.session_manager.run():
        turn = await build_turn_tools([SERVER], connect, 5)
        plain = await call_tool(
            turn, "mcparr_lookup", '{"region": "eu"}', connect=connect, call_timeout_s=5,
            max_chars=999,
        )  # fmt: skip
        assert plain.ok and "looked up eu" in plain.text
        need = await call_tool(
            turn, "mcparr_search", '{"region": "eu"}', connect=connect, call_timeout_s=5,
            max_chars=999,
        )  # fmt: skip
        assert need.need is not None, need.text
        done = await resume_tool(
            turn, "mcparr_search", '{"region": "eu"}', need.need.state,
            need.need.question.options[0].answer, connect=connect, call_timeout_s=5,
            max_chars=999,
        )  # fmt: skip
    assert done.ok and "searched eu: accept" in done.text
