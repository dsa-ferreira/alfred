"""The toolset contract: what every group of tools the model can call has in common.

The agent loop and every tool group depend on it rather than on each other.

A :class:`Toolset` is one source of tools: alfred's own (web, reminders,
memory, ...) or the configured MCP servers. The agent opens each toolset once
per turn; ``open`` returns ``None`` when the toolset isn't offered on that
turn (most local tools are for chat turns only). An open toolset holds what
it needs for that turn, is told when a new attempt starts (after a brain
fallback), and handles the calls to its tools.

```
turn:     opened = [Confirmed(Gated(t.open(turn), user.tools), user.confirm)
                    for t in toolsets], minus the Nones
attempt:  ctx = AttemptContext(...); o.new_attempt(ctx) for each opened
call:     dispatch(opened, name, args, ctx)  # first that handles the name
answer:   dispatch_resume(opened, name, args, need.state, answer, ctx)
```

Every opened toolset is wrapped in ``Gated`` with the turn user's
``AllowList``: a tool the list doesn't match is neither offered nor
callable. No toolset checks the list itself, except to leave out prompt
notes about tools the list hides. Outside that, ``Confirmed`` holds
back the calls the user's confirm list matches until the user says yes.

A call may return a need for the user's answer (``ToolOutcome.need``)
instead of a result. The turn then waits; once answered, the same toolset's
``resume`` gets the call again with the answer and returns its result, as if
the call had just finished.

The order of the toolsets is the order their definitions are offered in, and
the first one that handles a name wins a clash.

Turns are in ``alfred.kernel.turn``; the allow and confirm lists, with
:class:`~alfred.kernel.access.Gated` and :class:`~alfred.kernel.access.Confirmed`,
in ``alfred.kernel.access``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from alfred.kernel.content import ModelImage
from alfred.kernel.questions import InputNeed
from alfred.kernel.turn import ChatTurn, TurnInput

log = logging.getLogger(__name__)

LOCAL_GROUP = "alfred"  # how alfred's own tools are grouped in the context-size line


@dataclass(frozen=True)
class ToolOutcome:
    ok: bool
    text: str
    # Sent to the model with the text, as image parts of the tool message.
    images: tuple[ModelImage, ...] = ()
    # Set instead of a result when the call waits for the user (``ok`` and
    # ``text`` are then unused).
    need: InputNeed | None = None

    @classmethod
    def waiting(cls, need: InputNeed) -> ToolOutcome:
        return cls(False, "waiting for the user's answer", need=need)


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


@dataclass(frozen=True)
class PromptNote:
    """A paragraph a toolset adds to the system prompt.

    ``log_label`` and ``log_chars``, when set, list it in the context-size
    line as ``<label> <chars>`` (for example a server's instructions).
    """

    text: str
    log_label: str | None = None
    log_chars: int = 0


@dataclass
class AttemptContext:
    """One attempt of a turn, as the toolsets see it."""

    turn: TurnInput
    # What the turn's user wrote in this attempt's context window, oldest first.
    user_texts: list[str] = field(default_factory=list)
    reset: bool = False

    def request_reset(self) -> None:
        """End the conversation once this attempt's reply is sent."""
        self.reset = True


class OpenToolset(Protocol):
    """A toolset opened for one turn."""

    @property
    def definitions(self) -> list[dict[str, Any]]: ...

    @property
    def prompt_notes(self) -> Sequence[PromptNote]: ...

    def group_of(self, name: str) -> str:
        """The group the tool ``name`` is counted under in the context-size line."""
        ...

    def new_attempt(self, attempt: AttemptContext) -> None: ...

    def handles(self, name: str) -> bool: ...

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome: ...

    async def resume(
        self,
        name: str,
        raw_arguments: Any,
        state: dict[str, Any],
        answer: Any,
        attempt: AttemptContext,
    ) -> ToolOutcome:
        """Go on with a call that returned ``need``, now that its user chose ``answer``."""
        ...


class Toolset(Protocol):
    async def open(self, turn: TurnInput) -> OpenToolset | None:
        """This turn's tools, or ``None`` if none are offered on it."""
        ...


class LocalToolset:
    """Base for alfred's own tools: fixed definitions, no prompt notes, no state.

    Subclasses set ``definitions`` and implement ``call``; ``offered`` says on
    which turns they are offered (every turn by default). ``open`` returns the
    toolset itself, so a subclass with per-turn state overrides it.
    """

    definitions: list[dict[str, Any]] = []
    prompt_notes: Sequence[PromptNote] = ()

    def offered(self, turn: TurnInput) -> bool:
        return True

    async def open(self, turn: TurnInput) -> OpenToolset | None:
        return self if self.offered(turn) else None

    def group_of(self, name: str) -> str:
        return LOCAL_GROUP

    def new_attempt(self, attempt: AttemptContext) -> None:
        return None

    def handles(self, name: str) -> bool:
        return any(d["function"]["name"] == name for d in self.definitions)

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        raise NotImplementedError

    async def resume(
        self,
        name: str,
        raw_arguments: Any,
        state: dict[str, Any],
        answer: Any,
        attempt: AttemptContext,
    ) -> ToolOutcome:
        # Only a toolset that returns a need has anything to resume.
        raise NotImplementedError


def chat_only(turn: TurnInput) -> bool:
    return isinstance(turn, ChatTurn)


async def dispatch(
    opened: Sequence[OpenToolset], name: str, raw_arguments: Any, attempt: AttemptContext
) -> ToolOutcome:
    """Route a call to the first open toolset that handles it."""
    toolset = _find(opened, name)
    if toolset is None:
        return unknown_tool(name)
    return await _safe_call(toolset, name, raw_arguments, attempt)


async def dispatch_resume(
    opened: Sequence[OpenToolset],
    name: str,
    raw_arguments: Any,
    state: dict[str, Any],
    answer: Any,
    attempt: AttemptContext,
) -> ToolOutcome:
    """Route an answered call back to the first open toolset that handles it, like ``dispatch``."""
    toolset = _find(opened, name)
    if toolset is None:
        return unknown_tool(name)
    try:
        return await toolset.resume(name, raw_arguments, state, answer, attempt)
    except Exception as exc:  # noqa: BLE001 - any failure is reported to the model
        log.exception("tool %s failed on resume", name)
        return ToolOutcome(False, f"Error: tool {name!r} failed: {type(exc).__name__}")


def unknown_tool(name: str) -> ToolOutcome:
    return ToolOutcome(False, f"Error: unknown tool {name!r}.")


def _find(opened: Sequence[OpenToolset], name: str) -> OpenToolset | None:
    """The first open toolset that handles ``name``."""
    return next((t for t in opened if t.handles(name)), None)


async def _safe_call(
    toolset: OpenToolset, name: str, raw_arguments: Any, attempt: AttemptContext
) -> ToolOutcome:
    """Run one call; any exception becomes an error result for the model."""
    try:
        return await toolset.call(name, raw_arguments, attempt)
    except Exception as exc:  # noqa: BLE001 - any failure is reported to the model
        log.exception("tool %s failed", name)
        return ToolOutcome(False, f"Error: tool {name!r} failed: {type(exc).__name__}")
