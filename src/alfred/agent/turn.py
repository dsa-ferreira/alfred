"""One AI turn.

```
open    = each toolset opened for the turn (those not offered on it drop out),
          gated by the turn user's allow-list
tools   = the open toolsets' definitions, in toolset order
msgs    = [system prompt for the user (+ today's date, the brain, the toolsets' notes)]
          + last N entries of the user's conversation (+ the ask or reminder input)
loop up to ai.max_requests_per_turn:
    reply = model(msgs, tools)          # on the brain current at the turn's start
    no tool calls -> done
    run each call in order through the first open toolset that handles it;
    errors become results; any call => side effects possible
fail "stopped after too many steps"
```

**Tools.** The agent names no tool: what is offered on which turn, and what
a call does, is up to the toolsets (``alfred.tools.base``) that ``app.py``
passes in, seen through the turn user's allow-list and confirm list
(``Gated`` inside ``Confirmed``). The names left are what the system prompt
describes.

**Context.** Each attempt reads the history again and tells every open
toolset it started (``new_attempt``), so per-attempt state such as the web
fetch allowlist starts fresh. A user's chat message is already in the
history when its turn runs (the worker records it at turn start), so it
reaches the model as the newest entry. An ``/ask`` prompt is only recorded if
the answer reaches the user, so it is added to the messages here, after the
time now like the history's messages; so is a due reminder, labelled with its
due time and the time of this attempt, so a retried turn shows how late it
really is.

**Fallback.** A non-retryable model error on a brain other than the startup
one makes the startup brain current. If no tool had run, the turn runs again
from the start on it; either way the result carries the :class:`Fallback`,
and the worker tells the user and the admin.

**Waiting for the user.** When a call returns a need for its user's answer,
the turn stops there and returns ``waiting``: the call, the calls after it in
the same reply (not run yet), the messages since the context (``tail``), the
request and call counters, and the conversation and newest entry its context
was built from. A resumed turn (``TurnInput.resume``) rebuilds the same
context (the system prompt is new), appends the tail, hands the answer to the
toolset that asked, runs the rest of that reply's calls, and carries on as
usual. A resumed turn has run tools, so it is never retried.
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
from alfred.agent.context import context_sizes, largest_tools
from alfred.agent.inline import ImageCache, InlineAttachments
from alfred.agent.messages import stamped, strip_stamp, to_messages
from alfred.agent.prompts import FactsBlock, KnownFact, brain_line, facts_block, system_prompt
from alfred.brains import Brain, Brains
from alfred.config import Config, User
from alfred.db.models import TurnToolCall
from alfred.history import History, HistoryEntry
from alfred.model import ModelFailed, ModelReply, ModelUnreachable, ToolCall, token_counts
from alfred.reminders.when import format_now, format_when
from alfred.tools.base import (
    AttemptContext,
    Confirmed,
    Gated,
    InputNeed,
    OpenToolset,
    Resume,
    ToolOutcome,
    Toolset,
    TurnInput,
    dispatch,
    dispatch_resume,
    parse_arguments,
)

log = logging.getLogger(__name__)

_EXCERPT_CHARS = 500


@dataclass(frozen=True)
class Fallback:
    """The chosen brain failed and alfred went back to the startup brain."""

    failed: str
    default: str


@dataclass(frozen=True)
class _Checkpoint:
    """Where a parked turn stopped, stored as JSON while it waits for its user's answer.

    ``call`` is the call that asked, ``call_index`` its place in its model
    reply and ``remaining`` the calls after it, not run yet; ``tail`` the
    messages after the context (assistant messages with tool calls, and the
    results so far); ``requests_used`` and ``seq`` the model requests and tool
    calls so far; ``conversation_id`` and ``upto_entry_id`` (``None`` for no
    entries) where the context came from, and ``entry_ids`` exactly which
    entries. Turns parked by 0.16.0 saved no ids: ``None``.
    """

    call: ToolCall
    call_index: int
    remaining: tuple[ToolCall, ...]
    tail: list[dict[str, Any]]
    requests_used: int
    seq: int
    conversation_id: int | None
    upto_entry_id: int | None
    reset: bool = False
    entry_ids: tuple[int, ...] | None = ()

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> _Checkpoint:
        """Read what :meth:`to_json` wrote, or what 0.16.0 wrote (no ``entry_ids``)."""
        entry_ids = data.get("entry_ids")
        return cls(
            call=_call_of(data["call"]),
            call_index=data["call_index"],
            remaining=tuple(_call_of(c) for c in data["remaining"]),
            tail=data["tail"],
            requests_used=data["requests_used"],
            seq=data["seq"],
            conversation_id=data["conversation_id"],
            upto_entry_id=data["upto_entry_id"],
            reset=bool(data.get("reset")),
            entry_ids=None if entry_ids is None else tuple(entry_ids),
        )

    def to_json(self) -> dict[str, Any]:
        """What is stored to resume the turn; the same keys 0.16.1 stores."""
        data = {
            "call": _call_json(self.call),
            "call_index": self.call_index,
            "remaining": [_call_json(c) for c in self.remaining],
            "tail": self.tail,
            "requests_used": self.requests_used,
            "seq": self.seq,
            "conversation_id": self.conversation_id,
            "upto_entry_id": self.upto_entry_id,
            "reset": self.reset,
        }
        if self.entry_ids is not None:
            data["entry_ids"] = list(self.entry_ids)
        return data


@dataclass(frozen=True)
class _Waiting:
    """A turn parked at a call that needs its user's answer (``need``, stored apart)."""

    need: InputNeed
    checkpoint: _Checkpoint


@dataclass(frozen=True)
class TurnResult:
    reply: str
    reset_requested: bool
    side_effects_possible: bool
    # Told to the user (and the admin) before the reply: the brain fell back.
    fallback: Fallback | None = None
    # Set when the turn parked for its user's answer; ``reply`` is then empty.
    waiting: _Waiting | None = None


class TurnFailed(Exception):
    """The turn produced no answer.

    ``reason`` is for logs and the queue row; ``notice`` is the short text
    the turn's user may be shown. ``retryable`` is true only when the AI was
    unreachable and no tool had run yet. ``fallback``, when set, is told in
    any case: the chosen brain failed in this turn.
    """

    def __init__(
        self,
        reason: str,
        notice: str,
        *,
        retryable: bool,
        side_effects_possible: bool,
        fallback: Fallback | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.notice = notice
        self.retryable = retryable
        self.side_effects_possible = side_effects_possible
        self.fallback = fallback


class Agent:
    def __init__(
        self,
        config: Config,
        model: _Model,
        history: History,
        sessions: async_sessionmaker[AsyncSession],
        *,
        brains: Brains,
        attachments: InlineAttachments | None,
        facts: _Facts,
        toolsets: Sequence[Toolset] = (),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._facts = facts
        self._attachments = attachments
        self._brains = brains
        self._model = model
        self._history = history
        self._sessions = sessions
        self._toolsets = list(toolsets)
        self._now = now
        self._zone = ZoneInfo(config.ai.timezone)
        self._times = _Times(self._zone)

    async def run(self, turn: TurnInput) -> TurnResult:
        opened: list[OpenToolset] = []
        for toolset in self._toolsets:
            open_toolset = await toolset.open(turn)
            if open_toolset is not None:
                gated = Gated(open_toolset, turn.user.tools)
                opened.append(Confirmed(gated, turn.user.confirm))
        definitions = [d for t in opened for d in t.definitions]
        this = _Turn(
            turn,
            opened,
            definitions,
            [n.text for t in opened for n in t.prompt_notes],
            frozenset(d["function"]["name"] for d in definitions),
            self._attachments.new_cache() if self._attachments is not None else None,
            await self._facts_block(turn),
        )

        fallback: Fallback | None = None
        while True:
            # The brain is read once per attempt: a switch made during the
            # turn applies from the next turn.
            brain = self._brains.current
            try:
                result = await self._attempt(this, brain, can_fall_back=fallback is None)
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
                fallback = Fallback(brain.name, default.name)
                if not failure.side_effects:
                    continue
                raise TurnFailed(
                    f"{failure.reason} (on brain {brain.name}; after a tool ran, not run again)",
                    notices.failed(turn.user.address, True),
                    retryable=False,
                    side_effects_possible=True,
                    fallback=fallback,
                ) from None
            except TurnFailed as failure:
                failure.fallback = failure.fallback or fallback
                raise
            return dataclasses.replace(result, fallback=fallback)

    async def _facts_block(self, turn: TurnInput) -> FactsBlock | None:
        """The user's facts for every attempt of this turn, read once."""
        memory = self._config.memory
        if not memory.facts_in_context:
            return None
        name = turn.user.name
        block = facts_block(name, await self._facts.list(name), memory.facts_context_max_chars)
        if block.shown < block.total:
            log.warning(
                "turn %s: %d of %d facts fit [memory] facts_context_max_chars %d",
                turn.queue_item_id,
                block.shown,
                block.total,
                memory.facts_context_max_chars,
            )
        return block

    async def _attempt(self, this: _Turn, brain: Brain, *, can_fall_back: bool) -> TurnResult:
        resume = this.input.resume
        checkpoint = _Checkpoint.from_json(resume.turn) if resume is not None else None
        state = await self._build_context(this, brain, checkpoint, can_fall_back=can_fall_back)
        if resume is not None and checkpoint is not None:
            parked = await self._go_on(this, state, checkpoint, resume)
            if parked is not None:
                return self._parked(state, parked)
        return await self._loop(this, state)

    async def _build_context(
        self, this: _Turn, brain: Brain, checkpoint: _Checkpoint | None, *, can_fall_back: bool
    ) -> _State:
        """The attempt's starting messages, and where they came from (for parking).

        The system prompt, the history (with the inline window's
        attachments), then the ask or reminder input. Every open toolset is
        told the attempt started.
        """
        turn = this.input
        now = self._now()
        entries = await self._entries(turn, checkpoint)
        state = _State(
            brain,
            can_fall_back and brain != self._brains.default,
            AttemptContext(turn, [e.text for e in entries if e.kind == "user"]),
        )
        state.messages.append(self._system_message(this, brain, now))
        inline = {}
        if self._attachments is not None and this.images is not None and turn.kind == "chat":
            inline = await self._attachments.build(entries, vision=brain.vision, cache=this.images)
        state.messages += to_messages(entries, self._zone, inline)
        line = turn.input_line(now, self._times)
        if line is not None:
            state.messages.append({"role": "user", "content": line})
        for open_toolset in this.opened:
            open_toolset.new_attempt(state.context)
        state.context_len = len(state.messages)
        await self._note_source(state, turn.user, entries, checkpoint)
        self._log_context(this, state)
        return state

    async def _loop(self, this: _Turn, state: _State) -> TurnResult:
        """Ask the model and run its calls until it answers, a call waits, or steps run out."""
        turn = this.input
        last = self._config.ai.max_requests_per_turn
        for n in range(state.requests + 1, last + 1):
            reply = await self._ask_model(state, this.definitions)
            state.requests = n
            log.info("turn %s request %d: %s", turn.queue_item_id, n, token_counts(reply.usage))
            if not reply.tool_calls:
                # A stamp copied from the history is not part of the answer.
                answer = strip_stamp((reply.content or "").strip())
                if not answer:
                    raise self._failed(state, "the model returned an empty reply")
                return TurnResult(answer, state.context.reset, state.side_effects)
            state.messages.append(reply.assistant_message())
            parked = await self._run_calls(this, state, reply.tool_calls, 0)
            if parked is not None:
                return self._parked(state, parked)
        raise TurnFailed(
            f"stopped after {last} model requests",
            notices.too_many_steps(turn.user.address, state.side_effects),
            retryable=False,
            side_effects_possible=state.side_effects,
        )

    async def _entries(self, turn: TurnInput, checkpoint: _Checkpoint | None) -> list[HistoryEntry]:
        limit = self._config.history.max_entries
        if checkpoint is None:
            # Without other parked turns' entries: they have no reply yet.
            return await self._history.recent(
                turn.user.name, limit, turn_item_id=turn.queue_item_id
            )
        # The entries the turn parked with, whatever was recorded since and
        # whichever other parked turns ended meanwhile.
        return await self._history.parked_window(
            turn.user.name,
            limit,
            conversation_id=checkpoint.conversation_id,
            upto_entry_id=checkpoint.upto_entry_id,
            entry_ids=checkpoint.entry_ids,
            turn_item_id=turn.queue_item_id,
        )

    def _system_message(self, this: _Turn, brain: Brain, now: datetime) -> dict[str, Any]:
        cfg = self._config
        turn = this.input
        user = turn.user
        others = [b.name for b in self._brains.brains if b != brain]
        flags = turn.prompt_flags()
        return {
            "role": "system",
            "content": system_prompt(
                cfg.ai.persona,
                name=user.name,
                address=user.address,
                ask=flags.ask,
                reminder=flags.reminder,
                reminder_quiet=flags.reminder_quiet,
                today=now.astimezone(self._zone).date(),
                tools=this.offered,
                others=[u.name for u in cfg.users if u.name != user.name],
                notes=this.notes,
                brain=brain_line(brain.name, brain.tag, others, this.offered),
                facts=this.facts.text if this.facts is not None else None,
            ),
        }

    async def _note_source(
        self,
        state: _State,
        user: User,
        entries: list[HistoryEntry],
        checkpoint: _Checkpoint | None,
    ) -> None:
        """Remember which conversation and entries the context came from."""
        state.entry_ids = tuple(e.entry_id for e in entries if e.entry_id is not None)
        if checkpoint is not None:
            state.conversation_id = checkpoint.conversation_id
            state.upto_entry_id = checkpoint.upto_entry_id
        elif entries:
            state.conversation_id = entries[-1].conversation_id
            state.upto_entry_id = entries[-1].entry_id
        else:
            state.conversation_id = await self._history.current_conversation_id(user.name)

    def _log_context(self, this: _Turn, state: _State) -> None:
        turn_id = this.input.queue_item_id
        facts = this.facts
        sizes = context_sizes(
            state.messages,
            this.opened,
            None if facts is None else (facts.shown, facts.total, len(facts.text)),
        )
        log.info("turn %s context: %s; brain %s", turn_id, sizes, state.brain.name)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("turn %s largest tools: %s", turn_id, largest_tools(this.definitions))

    async def _go_on(
        self, this: _Turn, state: _State, saved: _Checkpoint, resume: Resume
    ) -> _Waiting | None:
        """Pick a parked turn up where it stopped: the answered call, then the rest of its reply."""
        state.messages += saved.tail
        state.side_effects = True  # tools ran before it parked
        state.seq = saved.seq
        state.requests = saved.requests_used
        if saved.reset:
            state.context.request_reset()
        call = saved.call
        remaining = saved.remaining
        state.seq += 1
        outcome = await dispatch_resume(
            this.opened, call.name, call.arguments, resume.state, resume.answer, state.context
        )
        await self._record(this.input, state.seq, call, outcome)
        if outcome.need is not None:  # the server asks again
            return self._waiting(state, call, saved.call_index, outcome.need, remaining)
        state.messages.append(_tool_message(call.id, outcome))
        return await self._run_calls(this, state, remaining, saved.call_index + 1)

    async def _run_calls(
        self, this: _Turn, state: _State, calls: Sequence[ToolCall], first_index: int
    ) -> _Waiting | None:
        """Run a reply's calls in order; stop at the first that needs the user's answer."""
        for i, call in enumerate(calls):
            outcome = await self._run_tool(this, call, state)
            if outcome.need is not None:
                return self._waiting(state, call, first_index + i, outcome.need, calls[i + 1 :])
            state.messages.append(_tool_message(call.id, outcome))
        return None

    def _waiting(
        self,
        state: _State,
        call: ToolCall,
        index: int,
        need: InputNeed,
        remaining: Sequence[ToolCall],
    ) -> _Waiting:
        return _Waiting(
            need,
            _Checkpoint(
                call,
                index,
                tuple(remaining),
                [_without_images(m) for m in state.messages[state.context_len :]],
                state.requests,
                state.seq,
                state.conversation_id,
                state.upto_entry_id,
                state.context.reset,
                state.entry_ids,
            ),
        )

    def _parked(self, state: _State, waiting: _Waiting) -> TurnResult:
        log.info(
            "turn %s parked at tool call #%d %s: waiting for %s's answer",
            state.context.turn.queue_item_id,
            waiting.checkpoint.seq,
            waiting.checkpoint.call.name,
            state.context.turn.user.name,
        )
        return TurnResult("", False, True, waiting=waiting)

    async def _ask_model(self, state: _State, definitions: list[dict[str, Any]]) -> ModelReply:
        address = state.context.turn.user.address
        try:
            return await self._model.complete(state.messages, definitions, model=state.brain.tag)
        except ModelUnreachable as exc:
            if not state.side_effects:
                raise TurnFailed(
                    str(exc),
                    notices.unreachable(address),
                    retryable=True,
                    side_effects_possible=False,
                ) from None
            raise TurnFailed(
                f"{exc} (after a tool ran; not retried)",
                notices.unreachable_mid(address),
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
            notices.failed(state.context.turn.user.address, state.side_effects),
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
        excerpt = outcome.text[:_EXCERPT_CHARS] + _images_note(outcome)
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


class _Model(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> ModelReply: ...


def _call_json(call: ToolCall) -> dict[str, Any]:
    return {"id": call.id, "name": call.name, "arguments": call.arguments}


def _call_of(data: dict[str, Any]) -> ToolCall:
    return ToolCall(data["id"], data["name"], data["arguments"])


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
    # Where the context ends and what it was built from, for parking.
    context_len: int = 0
    requests: int = 0
    conversation_id: int | None = None
    upto_entry_id: int | None = None
    entry_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class _Turn:
    """What stays the same across a turn's attempts."""

    input: TurnInput
    opened: list[OpenToolset]
    definitions: list[dict[str, Any]]
    notes: list[str]
    # The names in ``definitions``: what the system prompt describes.
    offered: frozenset[str]
    # The turn's attachment images, kept across attempts.
    images: ImageCache | None = None
    # The user's facts, when they are in the context.
    facts: FactsBlock | None = None


class _Facts(Protocol):
    """Where the turn reads its user's facts (``alfred.memory.facts.FactStore``)."""

    async def list(self, user_name: str) -> Sequence[KnownFact]: ...


def _tool_message(call_id: str, outcome: ToolOutcome) -> dict[str, Any]:
    """The result for the model; with images, a list of parts."""
    if not outcome.images:
        return {"role": "tool", "tool_call_id": call_id, "content": outcome.text}
    parts = [{"type": "text", "text": outcome.text}, *(i.part() for i in outcome.images)]
    return {"role": "tool", "tool_call_id": call_id, "content": parts}


def _without_images(message: dict[str, Any]) -> dict[str, Any]:
    """A message to keep while its turn waits: a tool result's images become a note."""
    content = message.get("content")
    if message.get("role") != "tool" or not isinstance(content, list):
        return message
    texts = [p["text"] for p in content if p.get("type") == "text"]
    n = len(content) - len(texts)
    note = f"[{n} image{'' if n == 1 else 's'} not kept while waiting]"
    return {**message, "content": "\n".join([*texts, note])}


def _images_note(outcome: ToolOutcome) -> str:
    """`` [1 image]``: what the excerpt keeps of a result's images, never their data."""
    n = len(outcome.images)
    return f" [{n} image{'' if n == 1 else 's'}]" if n else ""


class _Times:
    """Times in the user's zone, for the ask and reminder input lines."""

    def __init__(self, zone: ZoneInfo) -> None:
        self._zone = zone

    def stamp(self, text: str, moment: datetime) -> str:
        return stamped(text, moment, self._zone)

    def when(self, moment: datetime) -> str:
        return format_when(moment, self._zone)

    def now(self, now: datetime, due: datetime) -> str:
        return format_now(now, due, self._zone)
