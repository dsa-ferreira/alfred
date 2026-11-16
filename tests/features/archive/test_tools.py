"""MemoryTools: search and recall as a toolset."""

from __future__ import annotations

from typing import Any

from alfred.features.archive.tools import _RECALL_CONVERSATION, _SEARCH_CONVERSATIONS, MemoryTools
from alfred.kernel.toolset import AttemptContext, ToolOutcome
from alfred.kernel.turn import AskTurn, ChatTurn, ReminderTurn
from tests.helpers import DIOGO

OWNER = ChatTurn("remember the chicken?", 1, user=DIOGO)


class FakeSearch:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def search(
        self, user: str, query: Any = None, after: Any = None, before: Any = None
    ) -> ToolOutcome:
        self.calls.append(("search", user, query, after, before))
        return ToolOutcome(True, "found")

    async def recall(self, user: str, conversation_id: Any) -> ToolOutcome:
        self.calls.append(("recall", user, conversation_id))
        return ToolOutcome(True, "recalled")


async def test_offered_on_owner_turns_only() -> None:
    tools = MemoryTools(FakeSearch())
    assert await tools.open(OWNER) is tools
    assert await tools.open(AskTurn("x", 2, "svc", user=DIOGO)) is None
    assert await tools.open(ReminderTurn("x", 3, user=DIOGO)) is None
    assert [d["function"]["name"] for d in tools.definitions] == [
        _SEARCH_CONVERSATIONS,
        _RECALL_CONVERSATION,
    ]


async def test_arguments_passed_on() -> None:
    search = FakeSearch()
    tools = MemoryTools(search)
    ctx = AttemptContext(OWNER)
    await tools.call(_SEARCH_CONVERSATIONS, '{"query": "chicken", "after": "2026-11-01"}', ctx)
    await tools.call(_RECALL_CONVERSATION, {"id": 41}, ctx)
    assert search.calls == [
        ("search", "diogo", "chicken", "2026-11-01", None),
        ("recall", "diogo", 41),
    ]


async def test_invalid_json() -> None:
    search = FakeSearch()
    outcome = await MemoryTools(search).call(_SEARCH_CONVERSATIONS, "{nope", AttemptContext(OWNER))
    assert not outcome.ok and outcome.text.startswith("Error: the arguments are not valid JSON")
    assert search.calls == []
