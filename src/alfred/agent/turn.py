"""One AI turn (design D5).

```
tools = web tools + alfred_* local tools (owner turns only) + tools of reachable MCP servers
msgs  = [system prompt (+ today's date, reachable servers' instructions)]
        + last N history entries (+ the ask or reminder input)
loop up to ai.max_requests_per_turn:
    reply = model(msgs, tools)          # on the brain current at the turn's start
    no tool calls -> done
    run each call in order; errors become results; any call => side effects possible
fail "stopped after too many steps"
```

A non-retryable model error on a brain other than the startup one makes the
startup brain current (change add-brain-switching, design D5). If no tool
had run, the turn runs again from the start on it; either way the owner gets
the fallback notice.

An owner message is already in the history when its turn runs (the worker
records it at turn start), so it reaches the model as the newest entry. An
``/ask`` prompt is only recorded if the answer reaches the owner, so it is
added to the messages here. So is a due reminder (change add-reminders,
design D5), labelled with its due time and the time of this attempt, so a
retried turn shows how late it really is.

Each attempt gets a fresh web state (change add-web-search, design D3): its
fetch allowlist starts with the URLs in the owner's messages in the context
window and in the ``/ask`` prompt; search results add theirs as they come.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.agent.brains import Brain, Brains
from alfred.agent.model import ModelFailed, ModelReply, ModelUnreachable, ToolCall, token_counts
from alfred.agent.prompts import brain_line, system_prompt
from alfred.config import Config
from alfred.db.models import TurnToolCall
from alfred.history import History, to_messages
from alfred.mcp.client import Connector
from alfred.mcp.registry import (
    Route,
    ServerInstructions,
    ToolOutcome,
    TurnTools,
    build_turn_tools,
    call_tool,
    parse_arguments,
)
from alfred.reminders.tools import NAMES as REMINDER_TOOLS
from alfred.reminders.when import format_now, format_when
from alfred.web.tools import NAMES as WEB_TOOLS
from alfred.web.tools import WebTurn

log = logging.getLogger(__name__)

END_CONVERSATION = "alfred_end_conversation"
EXCERPT_CHARS = 500
LOCAL_TOOLS = "alfred"  # how alfred's own tools are grouped in the context-size line
LARGEST_TOOLS = 5

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

SEARCH_CONVERSATIONS = "alfred_search_conversations"
RECALL_CONVERSATION = "alfred_recall_conversation"

SEARCH_CONVERSATIONS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SEARCH_CONVERSATIONS,
        "description": (
            "Search earlier, archived conversations with the owner by topic, by date, or "
            "both. Use when the owner refers to something discussed before ('remember "
            "when…', 'that recipe we talked about last week', 'what did we talk about on "
            "Tuesday?'). Turn relative dates into after/before (YYYY-MM-DD, inclusive) using "
            "today's date from the system prompt. If one result is clearly the best match, "
            "recall it and tell the owner which conversation you picked (its title and "
            "date). If several match about equally, ask the owner which one they meant."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What the conversation was about, in a few words.",
                },
                "after": {
                    "type": "string",
                    "description": "Earliest date, YYYY-MM-DD, inclusive.",
                },
                "before": {
                    "type": "string",
                    "description": "Latest date, YYYY-MM-DD, inclusive.",
                },
            },
        },
    },
}

RECALL_CONVERSATION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": RECALL_CONVERSATION,
        "description": (
            "Bring an archived conversation, by its number, into this conversation so you "
            "can use what was said. Use when the owner names a conversation number or after "
            "search found the one they mean."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "The conversation number, e.g. 41."}
            },
            "required": ["id"],
        },
    },
}


SET_MODEL = "alfred_set_model"


def set_model_tool(names: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": SET_MODEL,
            "description": (
                "Switch the model that answers the owner, from their next message on. The "
                "owner may call it your model or your brain (for example 'change brains to "
                "gemma', 'set your brain to bonsai', 'use the bonsai model'). Call this ONLY "
                "when the owner asks to switch. The system prompt says which brain you are "
                "running on. After calling it, reply with a short confirmation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "enum": names,
                        "description": "The brain to switch to.",
                    }
                },
                "required": ["model"],
            },
        },
    }


class Model(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> ModelReply: ...


class WebTools(Protocol):
    """Web search and fetch (``alfred.web.tools.WebTools``)."""

    definitions: list[dict[str, Any]]

    def new_turn(self, texts: Iterable[str] = ()) -> WebTurn: ...

    async def call(self, turn: WebTurn, name: str, raw_arguments: Any) -> ToolOutcome: ...


class ReminderTools(Protocol):
    """Setting, listing and cancelling reminders (``alfred.reminders.tools.ReminderTools``)."""

    definitions: list[dict[str, Any]]

    async def call(self, name: str, raw_arguments: Any) -> ToolOutcome: ...


class MemoryTools(Protocol):
    """Search and recall of archived conversations (``alfred.memory.service.Memory``)."""

    async def search(
        self, query: Any = None, after: Any = None, before: Any = None
    ) -> ToolOutcome: ...

    async def recall(self, conversation_id: Any) -> ToolOutcome: ...


@dataclass(frozen=True)
class TurnInput:
    kind: Literal["owner", "ask", "reminder"]
    text: str
    queue_item_id: int
    source: str | None = None  # the service, for ask turns
    # For reminder turns: the reminder's number and when it was due.
    reminder_id: int | None = None
    due_at: datetime | None = None


@dataclass(frozen=True)
class TurnResult:
    reply: str
    reset_requested: bool
    side_effects_possible: bool
    # Sent to the owner before the reply: the chosen brain failed (design D5).
    fallback_notice: str | None = None


class TurnFailed(Exception):
    """The turn produced no answer.

    ``reason`` is for logs and the queue row; ``notice`` is the short text
    the owner may be shown. ``retryable`` is true only when the AI was
    unreachable and no tool had run yet. ``fallback_notice``, when set, is
    sent to the owner in any case: the chosen brain failed in this turn.
    """

    def __init__(
        self,
        reason: str,
        notice: str,
        *,
        retryable: bool,
        side_effects_possible: bool,
        fallback_notice: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.notice = notice
        self.retryable = retryable
        self.side_effects_possible = side_effects_possible
        self.fallback_notice = fallback_notice


class _BrainFailed(Exception):
    """A non-retryable model error on a brain that can still fall back."""

    def __init__(self, reason: str, side_effects: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.side_effects = side_effects


@dataclass
class _State:
    brain: Brain
    can_fall_back: bool
    side_effects: bool = False
    reset: bool = False
    seq: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    web: WebTurn | None = None


class Agent:
    def __init__(
        self,
        config: Config,
        model: Model,
        history: History,
        sessions: async_sessionmaker[AsyncSession],
        connect: Connector,
        *,
        memory: MemoryTools | None = None,
        web: WebTools | None = None,
        reminders: ReminderTools | None = None,
        brains: Brains | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._brains = brains or Brains(config.ai.model, config.ai.models)
        self._model = model
        self._history = history
        self._sessions = sessions
        self._connect = connect
        self._memory = memory
        self._web = web
        self._reminders = reminders
        self._now = now
        self._zone = ZoneInfo(config.ai.timezone)

    async def run(self, turn: TurnInput) -> TurnResult:
        cfg = self._config
        tools = await build_turn_tools(
            cfg.mcp_servers,
            self._connect,
            cfg.mcp.list_timeout_s,
            instructions_max_chars=cfg.mcp.instructions_max_chars,
        )
        definitions = list(tools.definitions)
        local: list[dict[str, Any]] = []
        if self._web is not None:
            local += self._web.definitions
        if turn.kind == "owner":
            local.append(END_CONVERSATION_TOOL)
            if self._memory is not None:
                local += [SEARCH_CONVERSATIONS_TOOL, RECALL_CONVERSATION_TOOL]
            if self._reminders is not None:
                local += self._reminders.definitions
            if len(self._brains.brains) > 1:
                local.append(set_model_tool(self._brains.names))
        definitions[:0] = local

        fallback_notice: str | None = None
        while True:
            # The brain is read once per attempt: a switch made during the
            # turn applies from the next turn (design D3).
            brain = self._brains.current
            try:
                result = await self._attempt(
                    turn, tools, definitions, brain, can_fall_back=fallback_notice is None
                )
            except _BrainFailed as failure:
                default = self._brains.default
                self._brains.current = default
                log.warning(
                    "turn %s: brain %s failed (%s); back on %s",
                    turn.queue_item_id,
                    brain.name,
                    failure.reason,
                    default.name,
                )
                fallback_notice = notices.fell_back(cfg.ai.owner_address, brain.name, default.name)
                if not failure.side_effects:
                    continue
                raise TurnFailed(
                    f"{failure.reason} (on brain {brain.name}; after a tool ran, not run again)",
                    notices.failed(cfg.ai.owner_address, True),
                    retryable=False,
                    side_effects_possible=True,
                    fallback_notice=fallback_notice,
                ) from None
            except TurnFailed as failure:
                failure.fallback_notice = failure.fallback_notice or fallback_notice
                raise
            return dataclasses.replace(result, fallback_notice=fallback_notice)

    async def _attempt(
        self,
        turn: TurnInput,
        tools: TurnTools,
        definitions: list[dict[str, Any]],
        brain: Brain,
        *,
        can_fall_back: bool,
    ) -> TurnResult:
        cfg = self._config
        others = [b.name for b in self._brains.brains if b != brain]
        state = _State(brain, can_fall_back and brain != self._brains.default)
        now = self._now()
        state.messages.append(
            {
                "role": "system",
                "content": system_prompt(
                    cfg.ai.persona,
                    owner_address=cfg.ai.owner_address,
                    ask=turn.kind == "ask",
                    reminder=turn.kind == "reminder",
                    today=now.astimezone(self._zone).date(),
                    unavailable=tools.unavailable,
                    instructions=tools.instructions,
                    brain=brain_line(brain.name, brain.tag, others),
                ),
            }
        )
        entries = await self._history.recent(cfg.history.max_entries)
        state.messages += to_messages(entries, self._zone)
        if turn.kind == "ask":
            state.messages.append({"role": "user", "content": f"[ask:{turn.source}] {turn.text}"})
        elif turn.kind == "reminder":
            state.messages.append({"role": "user", "content": self._reminder_input(turn, now)})
        if self._web is not None:
            # Only text the owner (or the asking service) wrote can open a page
            # without a search; replies, notices and recalls can't.
            seeds = [e.text for e in entries if e.kind == "owner"]
            if turn.kind == "ask":
                seeds.append(turn.text)
            state.web = self._web.new_turn(seeds)

        log.info(
            "turn %s context: %s; brain %s",
            turn.queue_item_id,
            context_sizes(state.messages, tools.instructions, definitions, tools.routes),
            brain.name,
        )
        if log.isEnabledFor(logging.DEBUG):
            log.debug("turn %s largest tools: %s", turn.queue_item_id, largest_tools(definitions))

        for n in range(1, cfg.ai.max_requests_per_turn + 1):
            reply = await self._ask_model(state, definitions)
            log.info("turn %s request %d: %s", turn.queue_item_id, n, token_counts(reply.usage))
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
            notices.too_many_steps(cfg.ai.owner_address, state.side_effects),
            retryable=False,
            side_effects_possible=state.side_effects,
        )

    def _reminder_input(self, turn: TurnInput, now: datetime) -> str:
        """``[reminder:#12 due Monday 9 November 2026, 17:00; now 17:12] call mum``."""
        due = turn.due_at or now
        return (
            f"[reminder:#{turn.reminder_id} due {format_when(due, self._zone)}; "
            f"now {format_now(now, due, self._zone)}] {turn.text}"
        )

    async def _ask_model(self, state: _State, definitions: list[dict[str, Any]]) -> ModelReply:
        try:
            return await self._model.complete(state.messages, definitions, model=state.brain.tag)
        except ModelUnreachable as exc:
            if not state.side_effects:
                raise TurnFailed(
                    str(exc),
                    notices.unreachable(self._config.ai.owner_address),
                    retryable=True,
                    side_effects_possible=False,
                ) from None
            raise TurnFailed(
                f"{exc} (after a tool ran; not retried)",
                notices.unreachable_mid(self._config.ai.owner_address),
                retryable=False,
                side_effects_possible=True,
            ) from None
        except ModelFailed as exc:
            if state.can_fall_back:
                raise _BrainFailed(str(exc), state.side_effects) from None
            raise self._failed(state, str(exc)) from None

    def _failed(self, state: _State, reason: str) -> TurnFailed:
        return TurnFailed(
            reason,
            notices.failed(self._config.ai.owner_address, state.side_effects),
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
        elif (
            call.name in (SEARCH_CONVERSATIONS, RECALL_CONVERSATION)
            and turn.kind == "owner"
            and self._memory is not None
        ):
            outcome = await self._memory_tool(self._memory, call)
        elif call.name in REMINDER_TOOLS and turn.kind == "owner" and self._reminders is not None:
            outcome = await self._reminder_tool(self._reminders, call)
        elif call.name == SET_MODEL and turn.kind == "owner" and len(self._brains.brains) > 1:
            outcome = self._set_model(turn, call)
        elif call.name in WEB_TOOLS and self._web is not None and state.web is not None:
            # Not cut to tool_result_max_chars: fetch sizes its own chunks.
            outcome = await self._web_tool(self._web, state.web, call)
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

    def _set_model(self, turn: TurnInput, call: ToolCall) -> ToolOutcome:
        arguments = parse_arguments(call.arguments)
        if isinstance(arguments, str):
            return ToolOutcome(False, arguments)
        name = arguments.get("model")
        before = self._brains.current
        result = self._brains.set(name) if isinstance(name, str) else "unknown"
        if result == "unknown":
            names = ", ".join(self._brains.names)
            return ToolOutcome(False, f"Error: unknown brain {name!r}. The brains are: {names}.")
        if result == "already":
            return ToolOutcome(True, f'The brain "{name}" is already in use; nothing changed.')
        log.info("turn %s: brain switched from %s to %s", turn.queue_item_id, before.name, name)
        return ToolOutcome(
            True,
            f'Switched to the brain "{name}" ({self._brains.tag}). It answers from the '
            "owner's next message on. Reply with a short confirmation.",
        )

    async def _web_tool(self, web: WebTools, web_turn: WebTurn, call: ToolCall) -> ToolOutcome:
        try:
            return await web.call(web_turn, call.name, call.arguments)
        except Exception as exc:  # noqa: BLE001 - any failure is reported to the model
            log.exception("tool %s failed", call.name)
            return ToolOutcome(False, f"Error: tool {call.name!r} failed: {type(exc).__name__}")

    async def _reminder_tool(self, reminders: ReminderTools, call: ToolCall) -> ToolOutcome:
        try:
            return await reminders.call(call.name, call.arguments)
        except Exception as exc:  # noqa: BLE001 - any failure is reported to the model
            log.exception("tool %s failed", call.name)
            return ToolOutcome(False, f"Error: tool {call.name!r} failed: {type(exc).__name__}")

    async def _memory_tool(self, memory: MemoryTools, call: ToolCall) -> ToolOutcome:
        arguments = parse_arguments(call.arguments)
        if isinstance(arguments, str):
            return ToolOutcome(False, arguments)
        try:
            if call.name == SEARCH_CONVERSATIONS:
                return await memory.search(
                    arguments.get("query"), arguments.get("after"), arguments.get("before")
                )
            return await memory.recall(arguments.get("id"))
        except Exception as exc:  # noqa: BLE001 - any failure is reported to the model
            log.exception("tool %s failed", call.name)
            return ToolOutcome(False, f"Error: tool {call.name!r} failed: {type(exc).__name__}")

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


# --- context size (logged, never sent) ---------------------------------------------


def _chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def context_sizes(
    messages: list[dict[str, Any]],
    instructions: list[ServerInstructions],
    definitions: list[dict[str, Any]],
    routes: dict[str, Route],
) -> str:
    """Where a turn's context comes from, in characters, without any of its text.

    ``messages`` is the system prompt followed by the history (and the ask
    input), as built at the start of the turn. Instruction sizes are part of
    the system prompt's size; tool sizes are each definition's JSON.
    """
    system = len(messages[0]["content"])
    line = f"system {system} chars"
    if instructions:
        parts = ", ".join(f"{i.server} {len(i.text)}" for i in instructions)
        line += f" (instructions: {parts})"

    by_group: dict[str, int] = {}
    for definition in definitions:
        name = definition["function"]["name"]
        route = routes.get(name)
        if route is not None:
            group = route.server.name
            by_group[group] = by_group.get(group, 0) + _chars(definition)
    local = sum(_chars(d) for d in definitions if d["function"]["name"] not in routes)
    if local:
        by_group[LOCAL_TOOLS] = local
    total = sum(by_group.values())
    line += f"; tools {len(definitions)} defs {total} chars"
    if by_group:
        line += " (" + ", ".join(f"{g} {n}" for g, n in by_group.items()) + ")"

    history = messages[1:]
    return line + f"; history {len(history)} entries {_chars(history)} chars"


def largest_tools(definitions: list[dict[str, Any]], count: int = LARGEST_TOOLS) -> str:
    """The ``count`` largest tool definitions as ``name size``, largest first."""
    sizes = sorted(((d["function"]["name"], _chars(d)) for d in definitions), key=lambda t: -t[1])
    return ", ".join(f"{name} {size}" for name, size in sizes[:count]) or "none"
