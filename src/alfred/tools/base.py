"""The toolset contract: what every group of tools the model can call has in common.

It depends on nothing else in alfred, so the agent loop and every tool group
(here in ``alfred.tools``, or in a feature package such as ``alfred.web``)
depend on it rather than on each other.

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

Every opened toolset is wrapped in :class:`Gated` with the turn user's
:class:`AllowList` (change add-multiple-users, design D3): a tool the list
doesn't match is neither offered nor callable. No toolset checks the list
itself, except to leave out prompt notes about tools the list hides.
Outside that, :class:`Confirmed` holds back the calls the user's confirm list
matches until the user says yes (change add-user-input-requests, design D2).

A call may return a need for the user's answer (``ToolOutcome.need``)
instead of a result. The turn then waits; once answered, the same toolset's
``resume`` gets the call again with the answer and returns its result, as if
the call had just finished.

The order of the toolsets is the order their definitions are offered in, and
the first one that handles a name wins a clash.

At runtime this module imports nothing else from alfred; ``User`` is only a
type here.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from alfred.config import User

log = logging.getLogger(__name__)

LOCAL_GROUP = "alfred"  # how alfred's own tools are grouped in the context-size line


@dataclass(frozen=True)
class AllowList:
    """A user's tool allow-list: exact names, ``prefix*`` patterns, or ``*`` for all."""

    entries: tuple[str, ...] = ()

    @classmethod
    def of(cls, *entries: str) -> AllowList:
        return cls(tuple(entries))

    def matches(self, name: str) -> bool:
        for entry in self.entries:
            if entry.endswith("*"):
                if name.startswith(entry[:-1]):
                    return True
            elif entry == name:
                return True
        return False

    def may_match_prefix(self, prefix: str) -> bool:
        """Whether any name starting with ``prefix`` (such as ``cookbook_``) can match."""
        return any(
            entry.startswith(prefix) or (entry.endswith("*") and prefix.startswith(entry[:-1]))
            for entry in self.entries
        )

    def exact(self, prefix: str = "") -> list[str]:
        """The entries that are exact names starting with ``prefix``."""
        return [e for e in self.entries if not e.endswith("*") and e.startswith(prefix)]


@dataclass(frozen=True)
class TurnInput:
    """What a turn answers: a user's chat message, an ``/ask`` prompt or a due reminder.

    ``user`` is whose turn it is: the sender, the target of the ``/ask``, or
    the reminder's recipient.
    """

    kind: Literal["chat", "ask", "reminder"]
    text: str
    queue_item_id: int
    source: str | None = None  # the service, for ask turns
    # For reminder turns: the reminder's number and when this occurrence was due.
    reminder_id: int | None = None
    due_at: datetime | None = None
    # For reminder turns: who set it, when not the recipient.
    set_by: str | None = None
    # For reminder turns: its repeat rule as shown ("every weekday at 08:00"),
    # and whether the turn may answer NO_REPLY (change add-recurring-reminders).
    repeat_label: str | None = None
    quiet_ok: bool = False
    user: User = field(kw_only=True)
    # Set when a parked turn goes on with its user's answer (change
    # add-user-input-requests, design D3).
    resume: Resume | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class Resume:
    """A parked turn's saved progress, and the answer it goes on with.

    ``turn`` is what the agent saved when it parked (JSON); ``state`` and
    ``answer`` go back to the toolset that asked.
    """

    turn: dict[str, Any]
    state: dict[str, Any]
    answer: Any


@dataclass(frozen=True)
class ModelImage:
    """An image ready for the model: already scaled down and encoded (change add-attachments)."""

    data: bytes
    mime: str  # image/jpeg or image/png

    def _data_url(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode('ascii')}"

    def part(self) -> dict[str, Any]:
        """The chat-completions content part that carries it."""
        return {"type": "image_url", "image_url": {"url": self._data_url()}}


@dataclass(frozen=True)
class Option:
    """A button: its label, and the JSON-able answer the toolset gets back when it's chosen."""

    label: str
    answer: Any


@dataclass(frozen=True)
class Question:
    """What a waiting call asks its user (change add-user-input-requests, design D2).

    ``title`` says what asked for it (the user's request, a service, a
    reminder) and is filled in by the worker; ``body`` is the question,
    ``detail`` preformatted text shown under it (the arguments as JSON).
    """

    body: str
    options: tuple[Option, ...]
    detail: str | None = None
    title: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "body": self.body,
            "detail": self.detail,
            "options": [{"label": o.label, "answer": o.answer} for o in self.options],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Question:
        return cls(
            body=data["body"],
            options=tuple(Option(o["label"], o["answer"]) for o in data["options"]),
            detail=data.get("detail"),
            title=data.get("title", ""),
        )


@dataclass(frozen=True)
class InputNeed:
    """A call that waits for its user's answer: the question, and what resuming it needs.

    ``state`` is JSON-able and handed back to the toolset's ``resume``.
    """

    question: Question
    state: dict[str, Any]


@dataclass(frozen=True)
class ToolOutcome:
    ok: bool
    text: str
    # Sent to the model with the text, as image parts of the tool message (design D5).
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
    return turn.kind == "chat"


class Gated:
    """An open toolset seen through an :class:`AllowList` (design D3).

    Definitions the list doesn't match are left out, and their names are not
    handled, so a call to one is an unknown tool. Everything else passes through.
    """

    def __init__(self, inner: OpenToolset, allow: AllowList) -> None:
        self.inner = inner
        self._allow = allow

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [d for d in self.inner.definitions if self._allow.matches(d["function"]["name"])]

    @property
    def prompt_notes(self) -> Sequence[PromptNote]:
        return self.inner.prompt_notes

    def group_of(self, name: str) -> str:
        return self.inner.group_of(name)

    def new_attempt(self, attempt: AttemptContext) -> None:
        self.inner.new_attempt(attempt)

    def handles(self, name: str) -> bool:
        return self._allow.matches(name) and self.inner.handles(name)

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        return await self.inner.call(name, raw_arguments, attempt)

    async def resume(
        self,
        name: str,
        raw_arguments: Any,
        state: dict[str, Any],
        answer: Any,
        attempt: AttemptContext,
    ) -> ToolOutcome:
        # The allow-list may have changed while the turn waited.
        if not self.handles(name):
            return _unknown_tool(name)
        return await self.inner.resume(name, raw_arguments, state, answer, attempt)


DO_IT = "Do it"
CANCEL = "Cancel"
_DECLINED = "The user declined this call; it did not run. Don't call it again unless they ask."
# The resume state of a confirmation.
_CONFIRM: dict[str, Any] = {"via": "confirm"}


class Confirmed:
    """An open toolset whose calls the user's confirm list matches wait for a yes (design D2).

    The outermost wrapper, around :class:`Gated`: it offers and handles what
    the gate does, so the confirm list never offers a tool. A matching call
    with arguments that parse returns a need (the tool name, the arguments
    as indented JSON, **Do it** and **Cancel**) instead of running; arguments
    that don't parse go straight through, as nothing can run. On resume,
    ``True`` makes the call and ``False`` declines it; any other state is the
    inner toolset's own question (an MCP server's) and passes through.
    """

    def __init__(self, inner: OpenToolset, confirm: AllowList) -> None:
        self.inner = inner
        self._confirm = confirm

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return self.inner.definitions

    @property
    def prompt_notes(self) -> Sequence[PromptNote]:
        return self.inner.prompt_notes

    def group_of(self, name: str) -> str:
        return self.inner.group_of(name)

    def new_attempt(self, attempt: AttemptContext) -> None:
        self.inner.new_attempt(attempt)

    def handles(self, name: str) -> bool:
        return self.inner.handles(name)

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        if self._confirm.matches(name):
            arguments = parse_arguments(raw_arguments)
            if isinstance(arguments, dict):
                return ToolOutcome.waiting(InputNeed(_confirm_question(name, arguments), _CONFIRM))
        return await self.inner.call(name, raw_arguments, attempt)

    async def resume(
        self,
        name: str,
        raw_arguments: Any,
        state: dict[str, Any],
        answer: Any,
        attempt: AttemptContext,
    ) -> ToolOutcome:
        if state.get("via") != _CONFIRM["via"]:
            return await self.inner.resume(name, raw_arguments, state, answer, attempt)
        if answer is True:
            return await self.inner.call(name, raw_arguments, attempt)
        return ToolOutcome(False, _DECLINED)


def _confirm_question(name: str, arguments: dict[str, Any]) -> Question:
    """The confirmation of a call: its model-facing name and its exact arguments."""
    return Question(
        f"Run {name} with these arguments?",
        (Option(DO_IT, True), Option(CANCEL, False)),
        detail=json.dumps(arguments, ensure_ascii=False, indent=2),
    )


def _unknown_tool(name: str) -> ToolOutcome:
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


async def dispatch(
    opened: Sequence[OpenToolset], name: str, raw_arguments: Any, attempt: AttemptContext
) -> ToolOutcome:
    """Route a call to the first open toolset that handles it."""
    toolset = _find(opened, name)
    if toolset is None:
        return _unknown_tool(name)
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
        return _unknown_tool(name)
    try:
        return await toolset.resume(name, raw_arguments, state, answer, attempt)
    except Exception as exc:  # noqa: BLE001 - any failure is reported to the model
        log.exception("tool %s failed on resume", name)
        return ToolOutcome(False, f"Error: tool {name!r} failed: {type(exc).__name__}")
