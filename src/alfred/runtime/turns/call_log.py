"""The audit log of turns' tool calls: ``turn_tool_calls``, the agent's ``_CallLog``."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.runtime.turns.tables import TurnToolCall


class ToolCallLog:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(
        self, queue_item_id: int, seq: int, tool: str, arguments: Any, ok: bool, excerpt: str
    ) -> None:
        """Record the ``seq``-th tool call of ``queue_item_id``'s turn, in its own transaction."""
        async with self._sessions.begin() as session:
            session.add(
                TurnToolCall(
                    queue_item_id=queue_item_id,
                    seq=seq,
                    tool=tool,
                    arguments=arguments,
                    ok=ok,
                    result_excerpt=excerpt,
                )
            )
