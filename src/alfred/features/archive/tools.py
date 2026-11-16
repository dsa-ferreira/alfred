"""``alfred_search_conversations`` and ``alfred_recall_conversation``, offered on chat turns."""

from __future__ import annotations

from typing import Any, Protocol

from alfred.kernel.toolset import (
    AttemptContext,
    LocalToolset,
    ToolOutcome,
    chat_only,
    parse_arguments,
)
from alfred.kernel.turn import TurnInput

_SEARCH_CONVERSATIONS = "alfred_search_conversations"
_RECALL_CONVERSATION = "alfred_recall_conversation"

_SEARCH_CONVERSATIONS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _SEARCH_CONVERSATIONS,
        "description": (
            "Search earlier, archived conversations with the user by topic, by date, or "
            "both. Use when the user refers to something discussed before ('remember "
            "when…', 'that recipe we talked about last week', 'what did we talk about on "
            "Tuesday?'). Turn relative dates into after/before (YYYY-MM-DD, inclusive) using "
            "today's date from the system prompt. If one result is clearly the best match, "
            "recall it and tell the user which conversation you picked (its title and "
            "date). If several match about equally, ask the user which one they meant."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What the conversation was about, in a few words.",
                },
                "after": {
                    "type": "string",
                    "description": "Earliest date, YYYY-MM-DD, inclusive.",
                },
                "before": {
                    "type": "string",
                    "description": "Latest date, YYYY-MM-DD, inclusive.",
                },
            },
        },
    },
}

_RECALL_CONVERSATION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _RECALL_CONVERSATION,
        "description": (
            "Bring an archived conversation, by its number, into this conversation so you "
            "can use what was said. Use when the user names a conversation number or after "
            "search found the one they mean."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "The conversation number, e.g. 41."}
            },
            "required": ["id"],
        },
    },
}


class Searcher(Protocol):
    async def search(
        self, user: str, query: Any = None, after: Any = None, before: Any = None
    ) -> ToolOutcome: ...

    async def recall(self, user: str, conversation_id: Any) -> ToolOutcome: ...


class MemoryTools(LocalToolset):
    """Search and recall of the turn user's conversations, offered on chat turns."""

    names = frozenset({_SEARCH_CONVERSATIONS, _RECALL_CONVERSATION})
    definitions = [_SEARCH_CONVERSATIONS_TOOL, _RECALL_CONVERSATION_TOOL]

    def __init__(self, search: Searcher) -> None:
        self._search = search

    def offered(self, turn: TurnInput) -> bool:
        return chat_only(turn)

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        arguments = parse_arguments(raw_arguments)
        if isinstance(arguments, str):
            return ToolOutcome(False, arguments)
        user = attempt.turn.user.name
        if name == _SEARCH_CONVERSATIONS:
            return await self._search.search(
                user, arguments.get("query"), arguments.get("after"), arguments.get("before")
            )
        return await self._search.recall(user, arguments.get("id"))
