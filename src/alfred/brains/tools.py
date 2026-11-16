"""``alfred_set_model``: the admin switches the brain (model) that answers everyone.

Offered on the admin's chat turns when there is more than one brain,
whatever other users' allow-lists say. The brains and the current one live
in ``alfred.brains.registry``; a switch applies alfred-wide from the next turn.
"""

from __future__ import annotations

import logging
from typing import Any

from alfred.brains.registry import Brains
from alfred.kernel.toolset import (
    AttemptContext,
    LocalToolset,
    ToolOutcome,
    chat_only,
    parse_arguments,
)
from alfred.kernel.turn import TurnInput

log = logging.getLogger(__name__)

_SET_MODEL = "alfred_set_model"


class BrainTools(LocalToolset):
    """``alfred_set_model``: on the admin's chat turns, when there is a brain to switch to."""

    names = frozenset({_SET_MODEL})

    def __init__(self, brains: Brains) -> None:
        self._brains = brains
        self.definitions = [_set_model_tool(brains.names)]

    def offered(self, turn: TurnInput) -> bool:
        return chat_only(turn) and turn.user.admin and len(self._brains.brains) > 1

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
            "next message on. Reply with a short confirmation.",
        )


def _set_model_tool(names: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _SET_MODEL,
            "description": (
                "Switch the model that answers, for every user, from the next message on. The "
                "user may call it your model or your brain (for example 'change brains to "
                "gemma', 'set your brain to bonsai', 'use the bonsai model'). Call this ONLY "
                "when the user asks to switch. The system prompt says which brain you are "
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
