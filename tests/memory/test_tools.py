"""MemoryTools: search and recall as a toolset."""

from __future__ import annotations

from typing import Any

from alfred.agent.toolset import AttemptContext, ToolOutcome
from alfred.agent.turn import TurnInput
from alfred.memory.search import RECALL_CONVERSATION, SEARCH_CONVERSATIONS, MemoryTools

OWNER = TurnInput("owner", "remember the chicken?", 1)


class FakeSearch:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def search(self, query: Any = None, after: Any = None, before: Any = None) -> ToolOutcome:
        self.calls.append(("search", query, after, before))
        return ToolOutcome(True, "found")

    async def recall(self, conversation_id: Any) -> ToolOutcome:
        self.calls.append(("recall", conversation_id))
        return ToolOutcome(True, "recalled")


async def test_offered_on_owner_turns_only() -> None:
    tools = MemoryTools(FakeSearch())
    assert await tools.open(OWNER) is tools
    assert await tools.open(TurnInput("ask", "x", 2, "svc")) is None
    assert await tools.open(TurnInput("reminder", "x", 3)) is None
    assert [d["function"]["name"] for d in tools.definitions] == [
        SEARCH_CONVERSATIONS,
        RECALL_CONVERSATION,
    ]


async def test_arguments_passed_on() -> None:
    search = FakeSearch()
    tools = MemoryTools(search)
    ctx = AttemptContext(OWNER)
    await tools.call(SEARCH_CONVERSATIONS, '{"query": "chicken", "after": "2026-11-01"}', ctx)
    await tools.call(RECALL_CONVERSATION, {"id": 41}, ctx)
    assert search.calls == [("search", "chicken", "2026-11-01", None), ("recall", 41)]


async def test_invalid_json() -> None:
    search = FakeSearch()
    outcome = await MemoryTools(search).call(SEARCH_CONVERSATIONS, "{nope", AttemptContext(OWNER))
    assert not outcome.ok and outcome.text.startswith("Error: the arguments are not valid JSON")
    assert search.calls == []
