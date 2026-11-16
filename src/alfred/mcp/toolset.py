"""The configured MCP servers as one toolset.

Opening it for a turn lists the servers the turn's user may use
(``build_turn_tools``), each with that user's token (change
add-multiple-users, design D8). A server none of the user's allow-list
entries can match is not contacted at all (design D3). The servers'
instructions, kept only when one of the server's tools passes the
allow-list, and the names of servers that couldn't be listed, reach the
system prompt as prompt notes. Calls go through the turn's routing map;
results are cut to ``[ai] tool_result_max_chars``.

A server's question to the user comes back as a need for input; ``resume``
repeats the call with the user's answer on a fresh connection (change
add-user-input-requests, design D6). A resumed turn opens the toolset anew,
so the routes it uses are those of that turn's listing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from alfred.mcp.client import Connector
from alfred.mcp.registry import (
    ServerInstructions,
    TurnTools,
    build_turn_tools,
    call_tool,
    resume_tool,
)
from alfred.tools.base import AllowList, AttemptContext, PromptNote, ToolOutcome, TurnInput

if TYPE_CHECKING:
    from alfred.config import Config, McpServer, User

log = logging.getLogger(__name__)


def _server_instructions(item: ServerInstructions) -> str:
    """One server's instructions, framed so the model maps tool names to its prefix."""
    return (
        f'Instructions from the tool server "{item.server}". Its tools are offered to you '
        f"as {item.server}_<tool>; where this text names a tool, use that prefixed name."
        f"\n\n{item.text}"
    )


def _unavailable_note(servers: Sequence[str]) -> str:
    names = ", ".join(servers)
    return (
        f"These tool servers are unavailable right now, so their tools are missing: {names}. "
        "If the request needs them, tell the user."
    )


def _prompt_notes(tools: TurnTools) -> list[PromptNote]:
    """Each server's instructions (logged by size), then the unavailable servers."""
    notes = [PromptNote(_server_instructions(i), i.server, len(i.text)) for i in tools.instructions]
    if tools.unavailable:
        notes.append(PromptNote(_unavailable_note(tools.unavailable)))
    return notes


def _keep_allowed(tools: TurnTools, allow: AllowList, user: str) -> None:
    """Drop instructions of servers with no allowed tool; warn about unknown exact names.

    The tools themselves are left for the gate (``alfred.tools.base.Gated``).
    """
    allowed = {route.server.name for name, route in tools.routes.items() if allow.matches(name)}
    tools.instructions = [i for i in tools.instructions if i.server in allowed]
    listed = {route.server.name for route in tools.routes.values()}
    for server in sorted(listed):
        for entry in allow.exact(f"{server}_"):
            if entry not in tools.routes:
                log.warning(
                    "user %s's tools list %r, which MCP server %s doesn't offer",
                    user,
                    entry,
                    server,
                )


class McpTools:
    def __init__(
        self,
        servers_for: Callable[[User], Sequence[McpServer]],
        connect: Connector,
        *,
        list_timeout_s: float,
        call_timeout_s: float,
        instructions_max_chars: int,
        max_chars: int,
    ) -> None:
        self._servers_for = servers_for
        self._connect = connect
        self._list_timeout_s = list_timeout_s
        self._call_timeout_s = call_timeout_s
        self._instructions_max_chars = instructions_max_chars
        self._max_chars = max_chars

    @classmethod
    def from_config(cls, config: Config, connect: Connector) -> McpTools:
        return cls(
            config.servers_for,
            connect,
            list_timeout_s=config.mcp.list_timeout_s,
            call_timeout_s=config.mcp.call_timeout_s,
            instructions_max_chars=config.mcp.instructions_max_chars,
            max_chars=config.ai.tool_result_max_chars,
        )

    async def open(self, turn: TurnInput) -> _OpenMcpTools:
        allow = turn.user.tools
        servers = [s for s in self._servers_for(turn.user) if allow.may_match_prefix(f"{s.name}_")]
        tools = await build_turn_tools(
            servers,
            self._connect,
            self._list_timeout_s,
            instructions_max_chars=self._instructions_max_chars,
        )
        _keep_allowed(tools, allow, turn.user.name)
        return _OpenMcpTools(self, tools)

    async def _call(self, tools: TurnTools, name: str, raw_arguments: Any) -> ToolOutcome:
        return await call_tool(
            tools,
            name,
            raw_arguments,
            connect=self._connect,
            call_timeout_s=self._call_timeout_s,
            max_chars=self._max_chars,
        )

    async def _resume(
        self, tools: TurnTools, name: str, raw_arguments: Any, state: dict[str, Any], answer: Any
    ) -> ToolOutcome:
        return await resume_tool(
            tools,
            name,
            raw_arguments,
            state,
            answer,
            connect=self._connect,
            call_timeout_s=self._call_timeout_s,
            max_chars=self._max_chars,
        )


class _OpenMcpTools:
    """One turn's listing: its tools, routes and prompt notes."""

    def __init__(self, owner: McpTools, tools: TurnTools) -> None:
        self._owner = owner
        self.tools = tools
        self.definitions = tools.definitions
        self.prompt_notes = _prompt_notes(tools)

    def group_of(self, name: str) -> str:
        return self.tools.routes[name].server.name

    def new_attempt(self, attempt: AttemptContext) -> None:
        return None

    def handles(self, name: str) -> bool:
        # Configured exclusions are handled too, so they get their own refusal.
        return name in self.tools.routes or name in self.tools.excluded

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        return await self._owner._call(self.tools, name, raw_arguments)

    async def resume(
        self,
        name: str,
        raw_arguments: Any,
        state: dict[str, Any],
        answer: Any,
        attempt: AttemptContext,
    ) -> ToolOutcome:
        return await self._owner._resume(self.tools, name, raw_arguments, state, answer)
