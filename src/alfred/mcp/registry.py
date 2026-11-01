"""The tools offered in one turn, and routing the model's calls to them.

At the start of every turn each configured server's tools are listed
concurrently. Servers that fail are reported as unavailable for that turn.
Tools are offered as ``<server>_<tool>``; the routing map built here is the
only way a call reaches a server, so prefixes are never parsed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from mcp.types import CallToolResult, TextContent, Tool

from alfred.config import McpServer
from alfred.mcp.client import Connector

log = logging.getLogger(__name__)

_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
MAX_TOOL_NAME = 64


def tool_name(server: str, tool: str) -> str:
    """The name the model sees: ``<server>_<tool>`` with disallowed characters as ``_``."""
    return _INVALID_CHARS.sub("_", f"{server}_{tool}")


@dataclass(frozen=True)
class Route:
    server: McpServer
    tool: str


@dataclass
class TurnTools:
    """What one turn may call."""

    definitions: list[dict[str, Any]] = field(default_factory=list)
    routes: dict[str, Route] = field(default_factory=dict)
    # Model-facing names of configured exclusions, refused without a network call.
    excluded: set[str] = field(default_factory=set)
    unavailable: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolOutcome:
    ok: bool
    text: str


async def build_turn_tools(
    servers: Sequence[McpServer], connect: Connector, list_timeout_s: float
) -> TurnTools:
    """List every server's tools concurrently and build this turn's tool set."""
    listings = await asyncio.gather(
        *(_list_tools(s, connect, list_timeout_s) for s in servers), return_exceptions=True
    )
    turn = TurnTools()
    for server, listing in zip(servers, listings, strict=True):
        turn.excluded |= {tool_name(server.name, t) for t in server.exclude_tools}
        if isinstance(listing, BaseException):
            log.warning(
                "MCP server %s is unavailable this turn: %s", server.name, _describe(listing)
            )
            turn.unavailable.append(server.name)
            continue
        exposed = {t.name for t in listing}
        for unknown in sorted(server.exclude_tools - exposed):
            log.warning(
                "MCP server %s has no tool %r (listed in exclude_tools)", server.name, unknown
            )
        for tool in listing:
            if tool.name in server.exclude_tools:
                continue
            _add(turn, server, tool)
    return turn


def _add(turn: TurnTools, server: McpServer, tool: Tool) -> None:
    name = tool_name(server.name, tool.name)
    if len(name) > MAX_TOOL_NAME:
        log.warning("skipping %s tool %r: name %r is too long", server.name, tool.name, name)
        return
    if name in turn.routes:
        other = turn.routes[name]
        log.warning(
            "skipping %s tool %r: name %r collides with %s tool %r",
            server.name,
            tool.name,
            name,
            other.server.name,
            other.tool,
        )
        return
    turn.routes[name] = Route(server, tool.name)
    turn.definitions.append(
        {
            "type": "function",
            "function": {
                "name": name,
                "description": tool.description or "",
                "parameters": tool.input_schema or {"type": "object", "properties": {}},
            },
        }
    )


async def _list_tools(server: McpServer, connect: Connector, limit_s: float) -> list[Tool]:
    async with asyncio.timeout(limit_s), connect(server, limit_s) as client:
        tools: list[Tool] = []
        cursor: str | None = None
        while True:
            result = await client.list_tools(cursor=cursor)
            tools += result.tools
            cursor = result.next_cursor
            if not cursor:
                return tools


async def call_tool(
    turn: TurnTools,
    name: str,
    raw_arguments: str | dict[str, Any] | None,
    *,
    connect: Connector,
    call_timeout_s: float,
    max_chars: int,
) -> ToolOutcome:
    """Run one model tool call. Every failure becomes an error result, never an exception."""
    route = turn.routes.get(name)
    if route is None:
        if name in turn.excluded:
            return ToolOutcome(
                False, f"Error: tool {name!r} is disabled in alfred's configuration."
            )
        return ToolOutcome(False, f"Error: unknown tool {name!r}.")

    arguments = parse_arguments(raw_arguments)
    if isinstance(arguments, str):
        return ToolOutcome(False, arguments)

    try:
        async with (
            asyncio.timeout(call_timeout_s),
            connect(route.server, call_timeout_s) as client,
        ):
            result = await client.call_tool(route.tool, arguments)
    except TimeoutError:
        return ToolOutcome(False, f"Error: tool {name!r} timed out after {call_timeout_s:g}s.")
    except Exception as exc:  # noqa: BLE001 - any failure is reported to the model
        log.warning("tool %s failed: %s", name, _describe(exc))
        return ToolOutcome(False, f"Error: tool {name!r} failed: {_describe(exc)}")
    return ToolOutcome(not result.is_error, truncate(result_text(result), max_chars))


def parse_arguments(raw: str | dict[str, Any] | None) -> dict[str, Any] | str:
    """The call's arguments as a dict, or an error message for the model."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return f"Error: the arguments are not valid JSON ({exc.msg})."
    if not isinstance(value, dict):
        return "Error: the arguments must be a JSON object."
    return value


def result_text(result: CallToolResult) -> str:
    """Structured content as JSON if present, otherwise the text blocks joined."""
    if result.structured_content is not None and not result.is_error:
        return json.dumps(result.structured_content, ensure_ascii=False)
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[{block.type} content omitted]")
    text = "\n".join(parts)
    if result.is_error:
        return f"Error: {text}" if text else "Error: the tool reported an error."
    return text


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n[truncated: {len(text) - max_chars} more characters]"


def _describe(exc: BaseException) -> str:
    """A short description of a failure, unwrapping exception groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, TimeoutError):
        return "timed out"
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
