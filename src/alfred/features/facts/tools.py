"""``alfred_remember`` and ``alfred_forget``, on the turn user's facts."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from alfred.features.facts.config import FactsConfig
from alfred.features.facts.store import FactsNotFound, FactStore
from alfred.kernel.toolset import (
    AttemptContext,
    LocalToolset,
    OpenToolset,
    PromptNote,
    ToolOutcome,
    chat_only,
    parse_arguments,
)
from alfred.kernel.turn import TurnInput

_REMEMBER = "alfred_remember"
_FORGET = "alfred_forget"

_REMEMBER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _REMEMBER,
        "description": (
            "Remember a lasting fact about the user for future conversations: use it when "
            "the user asks you to remember something about themselves, or corrects something "
            "you know about them. Not for one-off tasks; use a reminder for something due "
            "at a time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The fact, one short sentence, e.g. 'is vegetarian'.",
                },
                "replaces": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Ids of the user's facts this one corrects or makes outdated.",
                },
            },
            "required": ["text"],
        },
    },
}

_FORGET_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _FORGET,
        "description": "Forget facts about the user, by their ids, when the user asks you to.",
        "parameters": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "The fact ids, e.g. [12].",
                }
            },
            "required": ["ids"],
        },
    },
}


class FactTools(LocalToolset):
    """``alfred_remember`` and, while facts are in the context, ``alfred_forget``.

    Offered on chat turns only. Both act on the turn user's facts.
    """

    names = frozenset({_REMEMBER, _FORGET})

    def __init__(self, facts: FactStore, memory: FactsConfig) -> None:
        self._facts = facts
        self._memory = memory

    def offered(self, turn: TurnInput) -> bool:
        return chat_only(turn)

    async def open(self, turn: TurnInput) -> OpenToolset | None:
        if not self.offered(turn):
            return None
        return _OpenFactTools(self, turn, forget=self._memory.facts_in_context)

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        arguments = parse_arguments(raw_arguments)
        if isinstance(arguments, str):
            return ToolOutcome(False, arguments)
        user = attempt.turn.user.name
        if name == _REMEMBER:
            return await self._remember(user, arguments.get("text"), arguments.get("replaces"))
        return await self._forget(user, arguments.get("ids"))

    @property
    def facts(self) -> FactStore:
        return self._facts

    async def _remember(self, user: str, value: Any, replaces: Any) -> ToolOutcome:
        if not isinstance(value, str) or not value.strip():
            return ToolOutcome(False, "Error: give the fact as non-empty `text`.")
        fact_text = value.strip()
        limit = self._memory.fact_max_chars
        if len(fact_text) > limit:
            return ToolOutcome(
                False,
                f"Error: a fact is at most {limit} characters and this one has "
                f"{len(fact_text)}. Say it shorter.",
            )
        ids = _parse_ids(replaces if replaces is not None else [])
        if ids is None:
            return ToolOutcome(False, "Error: `replaces` must be a list of fact ids.")
        try:
            fact_id, replaced = await self.facts.add(user, fact_text, "told", replaces=ids)
        except FactsNotFound as exc:
            return ToolOutcome(
                False,
                f"Error: fact ids not found: {_ids(exc.ids)}. Nothing was saved or replaced.",
            )
        # Embedded now, so its duplicate check is ready once the queue is quiet.
        # The check itself needs a model request: never inside this turn.
        await self.facts.embed(fact_id)
        saved = f"Saved as fact {fact_id}."
        if replaced:
            saved += f" Replaced {_ids(f.id for f in replaced)}."
        return ToolOutcome(True, saved)

    async def _forget(self, user: str, value: Any) -> ToolOutcome:
        ids = _parse_ids(value)
        if not ids:
            return ToolOutcome(False, "Error: give the ids of the facts to forget, e.g. [12].")
        removed, missing = await self.facts.forget(user, ids)
        parts = []
        if removed:
            parts.append(f"Forgot {_ids(f.id for f in removed)}.")
        if missing:
            parts.append(f"Not found: {_ids(missing)}.")
        return ToolOutcome(bool(removed), " ".join(parts))


def _facts_note(remember: bool, forget: bool) -> str | None:
    """How the ids in the facts block are used, naming only the tools offered."""
    uses = [
        *([f"`{_FORGET}` to forget facts the user wants forgotten"] if forget else []),
        *([f"`replaces` of `{_REMEMBER}` when a fact corrects one"] if remember else []),
    ]
    if not uses:
        return None
    return (
        "The numbers in square brackets in what you know about the user are fact ids: "
        f"use them in {' and '.join(uses)}."
    )


class _OpenFactTools(LocalToolset):
    names = FactTools.names

    def __init__(self, tools: FactTools, turn: TurnInput, *, forget: bool) -> None:
        self._tools = tools
        self.definitions = [_REMEMBER_TOOL, *([_FORGET_TOOL] if forget else [])]
        if forget:
            # Only facts in the context have ids the model can see.
            allow = turn.user.tools
            note = _facts_note(allow.matches(_REMEMBER), allow.matches(_FORGET))
            self.prompt_notes = (PromptNote(note),) if note else ()

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        return await self._tools.call(name, raw_arguments, attempt)


def _parse_ids(value: Any) -> list[int] | None:
    if isinstance(value, int | str | float) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list):
        return None
    ids: list[int] = []
    for item in value:
        if isinstance(item, bool):
            return None
        if isinstance(item, float) and item.is_integer():
            item = int(item)
        if isinstance(item, str) and item.strip().lstrip("#").isdigit():
            item = int(item.strip().lstrip("#"))
        if not isinstance(item, int):
            return None
        ids.append(item)
    return ids


def _ids(ids: Iterable[int]) -> str:
    return ", ".join(str(i) for i in ids)
