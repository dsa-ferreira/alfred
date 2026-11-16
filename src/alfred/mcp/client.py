"""Opening MCP client connections (SDK v2, Streamable HTTP, bearer token).

See "MCP SDK v2 client notes" in AGENTS.md for the APIs used here.

Every client declares form elicitation: alfred drives a server's questions
itself, through the session-level ``call_tool`` with
``allow_input_required=True``, so the callback that declares the capability is
never meant to be reached.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx2
from mcp import Client
from mcp.client.context import ClientRequestContext
from mcp.client.streamable_http import streamable_http_client
from mcp.types import INVALID_REQUEST, ElicitRequestParams, ElicitResult, ErrorData

from alfred.config import McpServer

log = logging.getLogger(__name__)

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
            client = _new_client(streamable_http_client(server.url, http_client=http))
            async with client:
                yield client

    return connect


async def _refuse_elicitation(
    context: ClientRequestContext, params: ElicitRequestParams
) -> ElicitResult | ErrorData:
    """Declares form elicitation; questions come back to alfred as ``InputRequiredResult``.

    Reached only if a server sends a standalone elicitation request (an
    older protocol), which alfred can't hold open for the user's answer.
    """
    log.warning("refused a standalone elicitation request: %s", params.message[:200])
    return ErrorData(code=INVALID_REQUEST, message="alfred can't answer this question mid-call")


def _new_client(target: Any) -> Client:
    """A client for ``target`` (a transport, or an in-process server in tests).

    ``cache=None``: tool lists are looked up fresh every turn.
    """
    return Client(target, cache=None, elicitation_callback=_refuse_elicitation)
