"""The brains (models) the owner can switch between, and the current one (design D1).

The brains are the ``[ai.models]`` aliases plus the ``[ai] model`` tag, which
is listed under its own tag when no alias names it. The startup brain is the
one whose tag is ``[ai] model``. The current brain lives only in memory: a
restart goes back to the startup brain.

:class:`BrainTools` offers ``alfred_set_model`` on owner turns when there is
more than one brain.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from alfred.agent.toolset import (
    AttemptContext,
    LocalToolset,
    ToolOutcome,
    owner_only,
    parse_arguments,
)

if TYPE_CHECKING:
    from alfred.agent.turn import TurnInput

log = logging.getLogger(__name__)

SET_MODEL = "alfred_set_model"


@dataclass(frozen=True)
class Brain:
    name: str
    tag: str


class Brains:
    def __init__(self, default_tag: str, models: Mapping[str, str] | None = None) -> None:
        brains = [Brain(alias, tag) for alias, tag in (models or {}).items()]
        if not any(b.tag == default_tag for b in brains):
            brains.insert(0, Brain(default_tag, default_tag))
        self.brains: tuple[Brain, ...] = tuple(brains)
        self.default = next(b for b in brains if b.tag == default_tag)
        self.current = self.default

    @property
    def names(self) -> list[str]:
        return [b.name for b in self.brains]

    @property
    def others(self) -> list[str]:
        """The brains other than the current one, in configured order."""
        return [b.name for b in self.brains if b != self.current]

    @property
    def tag(self) -> str:
        return self.current.tag

    @property
    def is_default(self) -> bool:
        return self.current == self.default

    def set(self, name: str) -> Literal["switched", "already", "unknown"]:
        brain = next((b for b in self.brains if b.name == name), None)
        if brain is None:
            return "unknown"
        if brain == self.current:
            return "already"
        self.current = brain
        return "switched"

    def fall_back(self) -> bool:
        """Make the startup brain current; false when it already was."""
        if self.is_default:
            return False
        self.current = self.default
        return True


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


class BrainTools(LocalToolset):
    """``alfred_set_model``: offered on owner turns when there is a brain to switch to."""

    def __init__(self, brains: Brains) -> None:
        self._brains = brains
        self.definitions = [set_model_tool(brains.names)]

    def offered(self, turn: TurnInput) -> bool:
        return owner_only(turn) and len(self._brains.brains) > 1

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        arguments = parse_arguments(raw_arguments)
        if isinstance(arguments, str):
            return ToolOutcome(False, arguments)
        brain = arguments.get("model")
        before = self._brains.current
        result = self._brains.set(brain) if isinstance(brain, str) else "unknown"
        if result == "unknown":
            names = ", ".join(self._brains.names)
            return ToolOutcome(False, f"Error: unknown brain {brain!r}. The brains are: {names}.")
        if result == "already":
            return ToolOutcome(True, f'The brain "{brain}" is already in use; nothing changed.')
        log.info(
            "turn %s: brain switched from %s to %s",
            attempt.turn.queue_item_id,
            before.name,
            brain,
        )
        return ToolOutcome(
            True,
            f'Switched to the brain "{brain}" ({self._brains.tag}). It answers from the '
            "owner's next message on. Reply with a short confirmation.",
        )
