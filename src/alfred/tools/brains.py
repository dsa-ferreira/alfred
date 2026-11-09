"""``alfred_set_model``: the owner switches the brain (model) that answers them.

Offered on owner turns when there is more than one brain. The brains and the
current one live in ``alfred.agent.brains``; a switch applies from the next
turn.
"""

from __future__ import annotations

import logging
from typing import Any

from alfred.agent.brains import Brains
from alfred.tools.base import (
    AttemptContext,
    LocalToolset,
    ToolOutcome,
    TurnInput,
    owner_only,
    parse_arguments,
)

log = logging.getLogger(__name__)

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
