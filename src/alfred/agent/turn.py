"""One AI turn (design D5).

```
open    = each toolset opened for the turn (those not offered on it drop out)
tools   = the open toolsets' definitions, in toolset order
msgs    = [system prompt (+ today's date, the brain, the toolsets' notes)]
          + last N history entries (+ the ask or reminder input)
loop up to ai.max_requests_per_turn:
    reply = model(msgs, tools)          # on the brain current at the turn's start
    no tool calls -> done
    run each call in order through the first open toolset that handles it;
    errors become results; any call => side effects possible
fail "stopped after too many steps"
```

The agent names no tool: what is offered on which turn, and what a call
does, is up to the toolsets (``alfred.tools.base``) that ``app.py``
passes in.

A non-retryable model error on a brain other than the startup one makes the
startup brain current (change add-brain-switching, design D5). If no tool
had run, the turn runs again from the start on it; either way the owner gets
the fallback notice. Each attempt reads the history again and tells every
open toolset it started (``new_attempt``), so per-attempt state such as the
web fetch allowlist starts fresh.

An owner message is already in the history when its turn runs (the worker
records it at turn start), so it reaches the model as the newest entry. An
``/ask`` prompt is only recorded if the answer reaches the owner, so it is
added to the messages here, after the time now like the history's
messages (change add-utility-tools, design D1). So is a due reminder (change add-reminders,
design D5), labelled with its due time and the time of this attempt, so a
retried turn shows how late it really is.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.agent.brains import Brain, Brains
from alfred.agent.context import context_sizes, largest_tools
from alfred.agent.model import ModelFailed, ModelReply, ModelUnreachable, ToolCall, token_counts
from alfred.agent.prompts import brain_line, system_prompt
from alfred.attachments.inline import ImageCache, InlineAttachments
from alfred.config import Config
from alfred.db.models import TurnToolCall
from alfred.history import History, stamped, strip_stamp, to_messages
from alfred.reminders.when import format_now, format_when
from alfred.tools.base import (
    AttemptContext,
    OpenToolset,
    ToolOutcome,
    Toolset,
    TurnInput,
    dispatch,
    parse_arguments,
)

log = logging.getLogger(__name__)

EXCERPT_CHARS = 500


class Model(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> ModelReply: ...


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
    context: AttemptContext
    side_effects: bool = False
    seq: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _Turn:
    """What stays the same across a turn's attempts."""

    input: TurnInput
    opened: list[OpenToolset]
    definitions: list[dict[str, Any]]
    notes: list[str]
    # The turn's attachment images, kept across attempts (change add-attachments).
    images: ImageCache | None = None


class Agent:
    def __init__(
        self,
        config: Config,
        model: Model,
        history: History,
        sessions: async_sessionmaker[AsyncSession],
        *,
        toolsets: Sequence[Toolset] = (),
        brains: Brains | None = None,
        attachments: InlineAttachments | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._attachments = attachments
        self._brains = brains or Brains.from_config(config.ai)
        self._model = model
        self._history = history
        self._sessions = sessions
        self._toolsets = list(toolsets)
        self._now = now
        self._zone = ZoneInfo(config.ai.timezone)

    async def run(self, turn: TurnInput) -> TurnResult:
        cfg = self._config
        opened: list[OpenToolset] = []
        for toolset in self._toolsets:
            open_toolset = await toolset.open(turn)
            if open_toolset is not None:
                opened.append(open_toolset)
        this = _Turn(
            turn,
            opened,
            [d for t in opened for d in t.definitions],
            [n.text for t in opened for n in t.prompt_notes],
            self._attachments.new_cache() if self._attachments is not None else None,
        )

        fallback_notice: str | None = None
        while True:
            # The brain is read once per attempt: a switch made during the
            # turn applies from the next turn (design D3).
            brain = self._brains.current
            try:
                result = await self._attempt(this, brain, can_fall_back=fallback_notice is None)
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

    async def _attempt(self, this: _Turn, brain: Brain, *, can_fall_back: bool) -> TurnResult:
        cfg = self._config
        turn = this.input
        others = [b.name for b in self._brains.brains if b != brain]
        now = self._now()
        entries = await self._history.recent(cfg.history.max_entries)
        state = _State(
            brain,
            can_fall_back and brain != self._brains.default,
            AttemptContext(turn, [e.text for e in entries if e.kind == "owner"]),
        )
        state.messages.append(
            {
                "role": "system",
                "content": system_prompt(
                    cfg.ai.persona,
                    owner_address=cfg.ai.owner_address,
                    ask=turn.kind == "ask",
                    reminder=turn.kind == "reminder",
                    today=now.astimezone(self._zone).date(),
                    notes=this.notes,
                    brain=brain_line(brain.name, brain.tag, others),
                ),
            }
        )
        inline = {}
        if self._attachments is not None and this.images is not None and turn.kind == "owner":
            inline = await self._attachments.build(entries, vision=brain.vision, cache=this.images)
        state.messages += to_messages(entries, self._zone, inline)
        if turn.kind == "ask":
            ask = f"[ask:{turn.source}] {turn.text}"
            state.messages.append({"role": "user", "content": stamped(ask, now, self._zone)})
        elif turn.kind == "reminder":
            state.messages.append({"role": "user", "content": self._reminder_input(turn, now)})
        for open_toolset in this.opened:
            open_toolset.new_attempt(state.context)

        log.info(
            "turn %s context: %s; brain %s",
            turn.queue_item_id,
            context_sizes(state.messages, this.opened),
            brain.name,
        )
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "turn %s largest tools: %s", turn.queue_item_id, largest_tools(this.definitions)
            )

        for n in range(1, cfg.ai.max_requests_per_turn + 1):
            reply = await self._ask_model(state, this.definitions)
            log.info("turn %s request %d: %s", turn.queue_item_id, n, token_counts(reply.usage))
            if not reply.tool_calls:
                # A stamp copied from the history is not part of the answer (design D2).
                answer = strip_stamp((reply.content or "").strip())
                if not answer:
                    raise self._failed(state, "the model returned an empty reply")
                return TurnResult(answer, state.context.reset, state.side_effects)
            state.messages.append(reply.assistant_message())
            for call in reply.tool_calls:
                outcome = await self._run_tool(this, call, state)
                state.messages.append(tool_message(call.id, outcome))

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

    async def _run_tool(self, this: _Turn, call: ToolCall, state: _State) -> ToolOutcome:
        # Conservative: any tool, read or write, makes a retry unsafe.
        state.side_effects = True
        state.seq += 1
        outcome = await dispatch(this.opened, call.name, call.arguments, state.context)
        await self._record(this.input, state.seq, call, outcome)
        return outcome

    async def _record(
        self, turn: TurnInput, seq: int, call: ToolCall, outcome: ToolOutcome
    ) -> None:
        arguments = parse_arguments(call.arguments)
        stored: Any = arguments if isinstance(arguments, dict) else {"_raw": call.arguments}
        excerpt = outcome.text[:EXCERPT_CHARS] + images_note(outcome)
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


def tool_message(call_id: str, outcome: ToolOutcome) -> dict[str, Any]:
    """The result for the model; with images, a list of parts (change add-attachments, D5)."""
    if not outcome.images:
        return {"role": "tool", "tool_call_id": call_id, "content": outcome.text}
    parts = [{"type": "text", "text": outcome.text}, *(i.part() for i in outcome.images)]
    return {"role": "tool", "tool_call_id": call_id, "content": parts}


def images_note(outcome: ToolOutcome) -> str:
    """`` [1 image]``: what the excerpt keeps of a result's images, never their data."""
    n = len(outcome.images)
    return f" [{n} image{'' if n == 1 else 's'}]" if n else ""
