"""Who may call what: the allow-list that gates a user's tools, and the confirm
list whose calls wait for the user's yes.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from alfred.kernel.questions import CANCEL, DO_IT, InputNeed, Option, Question
from alfred.kernel.toolset import (
    AttemptContext,
    OpenToolset,
    PromptNote,
    ToolOutcome,
    parse_arguments,
    unknown_tool,
)

_DECLINED = "The user declined this call; it did not run. Don't call it again unless they ask."
# The resume state of a confirmation.
_CONFIRM: dict[str, Any] = {"via": "confirm"}


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


class Gated:
    """An open toolset seen through an :class:`AllowList`.

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
            return unknown_tool(name)
        return await self.inner.resume(name, raw_arguments, state, answer, attempt)


class Confirmed:
    """An open toolset whose calls the user's confirm list matches wait for a yes.

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
