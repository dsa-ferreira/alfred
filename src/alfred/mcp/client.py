"""Opening MCP client connections (SDK v2, Streamable HTTP, bearer token).

See "MCP SDK v2 client notes" in AGENTS.md for the APIs used here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from alfred.config import McpServer

# Opens a connected client to one server; ``timeout`` bounds each HTTP request.
Connector = Callable[[McpServer, float], AbstractAsyncContextManager[Client]]


def http_connector(transport: httpx2.AsyncBaseTransport | None = None) -> Connector:
    """The production connector. ``transport`` is for tests (e.g. ASGI)."""

    @asynccontextmanager
    async def connect(server: McpServer, http_timeout_s: float) -> AsyncIterator[Client]:
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {server.token.get_secret_value()}"},
            timeout=httpx2.Timeout(http_timeout_s),
            transport=transport,
        ) as http:
            # cache=None: tool lists are looked up fresh every turn.
            client = Client(streamable_http_client(server.url, http_client=http), cache=None)
            async with client:
                yield client

    return connect
