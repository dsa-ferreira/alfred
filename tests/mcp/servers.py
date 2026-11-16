"""In-process MCP SDK v2 servers and a connector for them."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from mcp import Client
from mcp.server import MCPServer
from mcp.server.elicitation import ElicitationResult
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve, Sample
from mcp.server.request_state import RequestStateSecurity
from mcp.types import CreateMessageResult, SamplingMessage, TextContent
from pydantic import BaseModel, SecretStr

from alfred.config import McpServer
from alfred.mcp.client import Connector, _new_client


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


COOKBOOK_INSTRUCTIONS = (
    "Units come from a fixed vocabulary: call list_units and use its codes. "
    "kcal and macros are whole-recipe totals."
)


def make_cookbook(instructions: str | None = COOKBOOK_INSTRUCTIONS) -> MCPServer:
    server = MCPServer("cookbook", instructions=instructions)

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


def config(
    name: str, exclude: frozenset[str] = frozenset(), *, use_instructions: bool = True
) -> McpServer:
    return McpServer(
        name, f"http://localhost/{name}/mcp", SecretStr(f"tok-{name}"), exclude, use_instructions
    )


def in_process(servers: Mapping[str, MCPServer | Exception]) -> Connector:
    """Connects to in-process servers; an Exception value means the server is down."""

    @asynccontextmanager
    async def connect(server: McpServer, http_timeout_s: float) -> AsyncIterator[Client]:
        target = servers[server.name]
        if isinstance(target, Exception):
            raise target
        async with _new_client(target) as client:
            yield client

    return connect


# --- servers that ask the user (change add-user-input-requests) -----------------
# Resolvers live at module level: the tools' string annotations name them.


class Nothing(BaseModel):
    pass


class Sure(BaseModel):
    sure: bool


class Quality(BaseModel):
    quality: Literal["1080p", "4K"]


class Name(BaseModel):
    name: str


class Two(BaseModel):
    a: bool
    b: bool


class Nine(BaseModel):
    pick: Literal["1", "2", "3", "4", "5", "6", "7", "8", "9"]


def confirm_add(title: str) -> Elicit[Nothing]:
    return Elicit(f"Add {title!r} to the library?", Nothing)


def ask_sure() -> Elicit[Sure]:
    return Elicit("Replace the existing file?", Sure)


def ask_quality() -> Elicit[Quality]:
    return Elicit("Which quality?", Quality)


def ask_name() -> Elicit[Name]:
    return Elicit("What name?", Name)


def ask_two() -> Elicit[Two]:
    return Elicit("Two things?", Two)


def ask_nine() -> Elicit[Nine]:
    return Elicit("Pick one of nine", Nine)


def ask_quality_after(
    confirmed: Annotated[ElicitationResult[Nothing], Resolve(confirm_add)],
) -> Elicit[Quality]:
    return Elicit("Which quality?", Quality)


def ask_sample() -> Sample:
    return Sample(
        [SamplingMessage(role="user", content=TextContent(type="text", text="hi"))],
        max_tokens=10,
    )


def make_asking(
    security: RequestStateSecurity | None = None, ran: list[str] | None = None
) -> MCPServer:
    """An mcparr whose tools ask the user before they act (form elicitation).

    ``ran`` records each tool body that runs.
    """
    server = MCPServer("mcparr", request_state_security=security)
    record = ran if ran is not None else []

    @server.tool()
    def add_media(
        title: str, answer: Annotated[ElicitationResult[Nothing], Resolve(confirm_add)]
    ) -> str:
        """Add a movie or series, after asking."""
        record.append("add_media")
        return f"added {title}" if answer.action == "accept" else f"not added ({answer.action})"

    @server.tool()
    def replace(answer: Annotated[ElicitationResult[Sure], Resolve(ask_sure)]) -> str:
        """Replace a file."""
        record.append("replace")
        return f"replace: {answer.action} {getattr(answer, 'data', None)}"

    @server.tool()
    def pick_quality(answer: Annotated[ElicitationResult[Quality], Resolve(ask_quality)]) -> str:
        """Pick a quality."""
        record.append("pick_quality")
        return f"quality: {answer.action} {getattr(answer, 'data', None)}"

    @server.tool()
    def add_in_quality(
        title: str, quality: Annotated[ElicitationResult[Quality], Resolve(ask_quality_after)]
    ) -> str:
        """Add, then ask the quality: two questions, one after the other."""
        record.append("add_in_quality")
        return f"added {title} in {getattr(quality, 'data', None)}"

    @server.tool()
    def name_it(answer: Annotated[ElicitationResult[Name], Resolve(ask_name)]) -> str:
        """Asks for free text."""
        record.append("name_it")
        return "named"

    @server.tool()
    def two_fields(answer: Annotated[ElicitationResult[Two], Resolve(ask_two)]) -> str:
        """Asks for two fields."""
        record.append("two_fields")
        return "two"

    @server.tool()
    def nine_choices(answer: Annotated[ElicitationResult[Nine], Resolve(ask_nine)]) -> str:
        """Asks for one of nine."""
        record.append("nine_choices")
        return "nine"

    @server.tool()
    def batched(
        a: Annotated[ElicitationResult[Sure], Resolve(ask_sure)],
        b: Annotated[ElicitationResult[Quality], Resolve(ask_quality)],
    ) -> str:
        """Asks two questions at once."""
        record.append("batched")
        return "batched"

    @server.tool()
    def summarise(result: Annotated[CreateMessageResult, Resolve(ask_sample)]) -> str:
        """Asks alfred to sample a model."""
        record.append("summarise")
        return "sampled"

    return server


def counting(connect: Connector, opened: list[str]) -> Connector:
    """``connect``, recording the server of every connection opened."""

    @asynccontextmanager
    async def wrapped(server: McpServer, http_timeout_s: float) -> AsyncIterator[Client]:
        opened.append(server.name)
        async with connect(server, http_timeout_s) as client:
            yield client

    return wrapped
