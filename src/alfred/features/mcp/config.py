"""``[mcp]`` and the ``[[mcp_servers]]`` alfred connects to."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import SecretStr

from alfred.infra.settings import PositiveFloat, PositiveInt, Section
from alfred.kernel.users import User


class McpConfig(Section):
    list_timeout_s: PositiveFloat = 10
    call_timeout_s: PositiveFloat = 60
    instructions_max_chars: PositiveInt = 2000


@dataclass(frozen=True)
class McpServer:
    name: str
    url: str
    token: SecretStr
    exclude_tools: frozenset[str] = field(default_factory=frozenset)
    use_server_instructions: bool = True


def servers_for(servers: Sequence[McpServer], user: User) -> tuple[McpServer, ...]:
    """The MCP servers with the token to use on ``user``'s turns."""
    return tuple(
        dataclasses.replace(s, token=user.mcp_tokens[s.name]) if s.name in user.mcp_tokens else s
        for s in servers
    )
