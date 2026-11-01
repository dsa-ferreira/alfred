"""One AI turn (design D5).

```
tools = alfred_* local tools (owner turns only) + tools of reachable MCP servers
msgs  = [system prompt] + last N history entries (+ the ask input)
loop up to ai.max_requests_per_turn:
    reply = model(msgs, tools)
    no tool calls -> done
    run each call in order; errors become results; any call => side effects possible
fail "stopped after too many steps"
```

An owner message is already in the history when its turn runs (the worker
records it at turn start), so it reaches the model as the newest entry. An
``/ask`` prompt is only recorded if the answer reaches the owner, so it is
added to the messages here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.model import ModelFailed, ModelReply, ModelUnreachable, ToolCall
from alfred.agent.prompts import system_prompt
from alfred.config import Config
from alfred.db.models import TurnToolCall
from alfred.history import History, to_messages
from alfred.mcp.client import Connector
from alfred.mcp.registry import (
    ToolOutcome,
    TurnTools,
    build_turn_tools,
    call_tool,
    parse_arguments,
)

log = logging.getLogger(__name__)

END_CONVERSATION = "alfred_end_conversation"
EXCERPT_CHARS = 500

END_CONVERSATION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": END_CONVERSATION,
        "description": (
            "End the current conversation and start a fresh one with no memory of it. "
            "Call this ONLY when the owner clearly says the conversation or topic is "
            "finished (for example 'thanks, we're done', 'that's all', 'new topic'). "
            "Never call it because a question was answered, on a simple 'thanks', or "
            "when unsure. After calling it, reply with a short closing line."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


class Model(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


@dataclass(frozen=True)
class TurnInput:
    kind: Literal["owner", "ask"]
    text: str
    queue_item_id: int
    source: str | None = None  # the service, for ask turns


@dataclass(frozen=True)
class TurnResult:
    reply: str
    reset_requested: bool
    side_effects_possible: bool


class TurnFailed(Exception):
    """The turn produced no answer.

    ``reason`` is for logs and the queue row; ``notice`` is the short text
    the owner may be shown. ``retryable`` is true only when the AI was
    unreachable and no tool had run yet.
    """

    def __init__(
        self, reason: str, notice: str, *, retryable: bool, side_effects_possible: bool
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.notice = notice
        self.retryable = retryable
        self.side_effects_possible = side_effects_possible


PARTIAL = " Some actions may have partly completed."


@dataclass
class _State:
    side_effects: bool = False
    reset: bool = False
    seq: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        config: Config,
        model: Model,
        history: History,
        sessions: async_sessionmaker[AsyncSession],
        connect: Connector,
    ) -> None:
        self._config = config
        self._model = model
        self._history = history
        self._sessions = sessions
        self._connect = connect

    async def run(self, turn: TurnInput) -> TurnResult:
        cfg = self._config
        tools = await build_turn_tools(cfg.mcp_servers, self._connect, cfg.mcp.list_timeout_s)
        definitions = list(tools.definitions)
        if turn.kind == "owner":
            definitions.insert(0, END_CONVERSATION_TOOL)

        state = _State()
        state.messages.append(
            {
                "role": "system",
                "content": system_prompt(
                    cfg.ai.system_prompt, ask=turn.kind == "ask", unavailable=tools.unavailable
                ),
            }
        )
        state.messages += to_messages(await self._history.recent(cfg.history.max_entries))
        if turn.kind == "ask":
            state.messages.append({"role": "user", "content": f"[ask:{turn.source}] {turn.text}"})

        for _ in range(cfg.ai.max_requests_per_turn):
            reply = await self._ask_model(state, definitions)
            if not reply.tool_calls:
                answer = (reply.content or "").strip()
                if not answer:
                    raise self._failed(state, "the model returned an empty reply")
                return TurnResult(answer, state.reset, state.side_effects)
            state.messages.append(reply.assistant_message())
            for call in reply.tool_calls:
                outcome = await self._run_tool(turn, tools, call, state)
                state.messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": outcome.text}
                )

        raise TurnFailed(
            f"stopped after {cfg.ai.max_requests_per_turn} model requests",
            "I stopped that request after too many steps."
            + (PARTIAL if state.side_effects else ""),
            retryable=False,
            side_effects_possible=state.side_effects,
        )

    async def _ask_model(self, state: _State, definitions: list[dict[str, Any]]) -> ModelReply:
        try:
            return await self._model.complete(state.messages, definitions)
        except ModelUnreachable as exc:
            if not state.side_effects:
                raise TurnFailed(
                    str(exc),
                    "The AI is unreachable.",
                    retryable=True,
                    side_effects_possible=False,
                ) from None
            raise TurnFailed(
                f"{exc} (after a tool ran; not retried)",
                "The AI became unreachable in the middle of that request." + PARTIAL,
                retryable=False,
                side_effects_possible=True,
            ) from None
        except ModelFailed as exc:
            raise self._failed(state, str(exc)) from None

    @staticmethod
    def _failed(state: _State, reason: str) -> TurnFailed:
        return TurnFailed(
            reason,
            "Sorry, that request failed." + (PARTIAL if state.side_effects else ""),
            retryable=False,
            side_effects_possible=state.side_effects,
        )

    async def _run_tool(
        self, turn: TurnInput, tools: TurnTools, call: ToolCall, state: _State
    ) -> ToolOutcome:
        # Conservative: any tool, read or write, makes a retry unsafe.
        state.side_effects = True
        state.seq += 1
        if call.name == END_CONVERSATION and turn.kind == "owner":
            state.reset = True
            outcome = ToolOutcome(
                True,
                "The conversation will be archived after your reply. "
                "Reply with a short closing line.",
            )
        else:
            outcome = await call_tool(
                tools,
                call.name,
                call.arguments,
                connect=self._connect,
                call_timeout_s=self._config.mcp.call_timeout_s,
                max_chars=self._config.ai.tool_result_max_chars,
            )
        await self._record(turn, state.seq, call, outcome)
        return outcome

    async def _record(
        self, turn: TurnInput, seq: int, call: ToolCall, outcome: ToolOutcome
    ) -> None:
        arguments = parse_arguments(call.arguments)
        stored: Any = arguments if isinstance(arguments, dict) else {"_raw": call.arguments}
        excerpt = outcome.text[:EXCERPT_CHARS]
        log.info(
            "turn %s tool call #%d %s %s -> %s: %s",
            turn.queue_item_id,
            seq,
            call.name,
            json.dumps(stored, ensure_ascii=False),
            "ok" if outcome.ok else "error",
            excerpt[:200],
        )
        async with self._sessions.begin() as session:
            session.add(
                TurnToolCall(
                    queue_item_id=turn.queue_item_id,
                    seq=seq,
                    tool=call.name,
                    arguments=stored,
                    ok=outcome.ok,
                    result_excerpt=excerpt,
                )
            )
