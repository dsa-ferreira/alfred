"""The configured MCP servers as one toolset.

Opening it for a turn lists every server (``build_turn_tools``). The servers'
instructions, and the names of servers that couldn't be listed, reach the
system prompt as prompt notes. Calls go through the turn's routing map;
results are cut to ``[ai] tool_result_max_chars``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from alfred.mcp.client import Connector
from alfred.mcp.registry import ServerInstructions, TurnTools, build_turn_tools, call_tool
from alfred.tools.base import AttemptContext, PromptNote, ToolOutcome, TurnInput

if TYPE_CHECKING:
    from alfred.config import Config, McpServer


def server_instructions(item: ServerInstructions) -> str:
    """One server's instructions, framed so the model maps tool names to its prefix."""
    return (
        f'Instructions from the tool server "{item.server}". Its tools are offered to you '
        f"as {item.server}_<tool>; where this text names a tool, use that prefixed name."
        f"\n\n{item.text}"
    )


def unavailable_note(servers: Sequence[str]) -> str:
    names = ", ".join(servers)
    return (
        f"These tool servers are unavailable right now, so their tools are missing: {names}. "
        "If the request needs them, tell the owner."
    )


def prompt_notes(tools: TurnTools) -> list[PromptNote]:
    """Each server's instructions (logged by size), then the unavailable servers."""
    notes = [PromptNote(server_instructions(i), i.server, len(i.text)) for i in tools.instructions]
    if tools.unavailable:
        notes.append(PromptNote(unavailable_note(tools.unavailable)))
    return notes


class McpTools:
    def __init__(
        self,
        servers: Sequence[McpServer],
        connect: Connector,
        *,
        list_timeout_s: float,
        call_timeout_s: float,
        instructions_max_chars: int,
        max_chars: int,
    ) -> None:
        self._servers = servers
        self._connect = connect
        self._list_timeout_s = list_timeout_s
        self._call_timeout_s = call_timeout_s
        self._instructions_max_chars = instructions_max_chars
        self._max_chars = max_chars

    @classmethod
    def from_config(cls, config: Config, connect: Connector) -> McpTools:
        return cls(
            config.mcp_servers,
            connect,
            list_timeout_s=config.mcp.list_timeout_s,
            call_timeout_s=config.mcp.call_timeout_s,
            instructions_max_chars=config.mcp.instructions_max_chars,
            max_chars=config.ai.tool_result_max_chars,
        )

    async def open(self, turn: TurnInput) -> OpenMcpTools:
        tools = await build_turn_tools(
            self._servers,
            self._connect,
            self._list_timeout_s,
            instructions_max_chars=self._instructions_max_chars,
        )
        return OpenMcpTools(self, tools)

    async def _call(self, tools: TurnTools, name: str, raw_arguments: Any) -> ToolOutcome:
        return await call_tool(
            tools,
            name,
            raw_arguments,
            connect=self._connect,
            call_timeout_s=self._call_timeout_s,
            max_chars=self._max_chars,
        )


class OpenMcpTools:
    """One turn's listing: its tools, routes and prompt notes."""

    def __init__(self, owner: McpTools, tools: TurnTools) -> None:
        self._owner = owner
        self.tools = tools
        self.definitions = tools.definitions
        self.prompt_notes = prompt_notes(tools)

    def group_of(self, name: str) -> str:
        return self.tools.routes[name].server.name

    def new_attempt(self, attempt: AttemptContext) -> None:
        return None

    def handles(self, name: str) -> bool:
        # Configured exclusions are handled too, so they get their own refusal.
        return name in self.tools.routes or name in self.tools.excluded

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        return await self._owner._call(self.tools, name, raw_arguments)
