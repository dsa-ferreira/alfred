"""``alfred_end_conversation``: the owner ends the current conversation."""

from __future__ import annotations

from typing import Any

from alfred.tools.base import AttemptContext, LocalToolset, ToolOutcome, TurnInput, owner_only

END_CONVERSATION = "alfred_end_conversation"

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


class ConversationTools(LocalToolset):
    """Offered on owner turns; the worker archives the conversation after the reply."""

    definitions = [END_CONVERSATION_TOOL]

    def offered(self, turn: TurnInput) -> bool:
        return owner_only(turn)

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        attempt.request_reset()
        return ToolOutcome(
            True,
            "The conversation will be archived after your reply. Reply with a short closing line.",
        )
