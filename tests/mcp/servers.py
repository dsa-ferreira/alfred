"""In-process MCP SDK v2 servers and a connector for them."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, SecretStr

from alfred.config import McpServer
from alfred.mcp.client import Connector


class Download(BaseModel):
    title: str
    progress: float


class Downloads(BaseModel):
    items: list[Download]


def make_mcparr() -> MCPServer:
    server = MCPServer("mcparr")

    @server.tool()
    def get_downloads() -> Downloads:
        """What's downloading right now."""
        return Downloads(items=[Download(title="Dune", progress=0.5)])

    @server.tool()
    def check_health() -> str:
        """Health of mcparr."""
        return "mcparr ok"

    @server.tool()
    def add_media(title: str) -> str:
        """Add a movie or series."""
        return f"added {title}"

    return server


def make_cookbook() -> MCPServer:
    server = MCPServer("cookbook")

    @server.tool()
    def get_recipe(recipe_id: int) -> str:
        """Get one recipe."""
        raise ToolError(f"recipe {recipe_id} does not exist")

    @server.tool()
    def delete_recipe(recipe_id: int) -> str:
        """Delete a recipe."""
        raise AssertionError("excluded tools must never be called")

    @server.tool()
    def check_health() -> str:
        """Health of cookbook."""
        return "cookbook ok"

    @server.tool()
    def big() -> str:
        """A large result."""
        return "x" * 10_000

    return server


def config(name: str, exclude: frozenset[str] = frozenset()) -> McpServer:
    return McpServer(name, f"http://localhost/{name}/mcp", SecretStr(f"tok-{name}"), exclude)


def in_process(servers: Mapping[str, MCPServer | Exception]) -> Connector:
    """Connects to in-process servers; an Exception value means the server is down."""

    @asynccontextmanager
    async def connect(server: McpServer, http_timeout_s: float) -> AsyncIterator[Client]:
        target = servers[server.name]
        if isinstance(target, Exception):
            raise target
        async with Client(target, cache=None) as client:
            yield client

    return connect
