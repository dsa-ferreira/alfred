"""Memory search and recall in a turn."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.prompts import NO_REPLY
from alfred.agent.turn import TurnInput
from alfred.db.models import Entry
from alfred.history import History
from alfred.memory.search import RECALL_CONVERSATION, SEARCH_CONVERSATIONS
from tests.agent.turn_helpers import agent, memory, owner_turn, tool_rows
from tests.helpers import FakeEmbedder, FakeModel, add_queue_item, calls, text
from tests.memory.test_search import CHICKEN, at, seed

# --- memory tools ------------------------------------------------------------------


async def test_memory_tools_offered_on_owner_turns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, memory=memory(sessions, FakeEmbedder())).run(
        await owner_turn(sessions, "hi")
    )
    assert {SEARCH_CONVERSATIONS, RECALL_CONVERSATION} <= model.tool_names()
    search = next(
        t["function"] for t in model.requests[0][1] if t["function"]["name"] == SEARCH_CONVERSATIONS
    )
    assert set(search["parameters"]["properties"]) == {"query", "after", "before"}
    assert "required" not in search["parameters"]
    assert "ask the owner which one they meant" in search["description"]
    assert "recall it and tell the owner which conversation you picked" in search["description"]


async def test_memory_tools_not_offered_on_ask_turns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(sessions, 41, "Chicken thighs recipe", "Lemon.", started=at(1))
    item = await add_queue_item(sessions, "ask", "x", "disk-check")
    model = FakeModel([calls((RECALL_CONVERSATION, '{"id": 41}')), text(NO_REPLY)])
    await agent(sessions, model, memory=memory(sessions, FakeEmbedder())).run(
        TurnInput("ask", "x", item, "disk-check")
    )
    assert not {SEARCH_CONVERSATIONS, RECALL_CONVERSATION} & model.tool_names()
    assert "unknown tool" in model.requests[1][0][-1]["content"]
    async with sessions() as session:
        assert list(await session.scalars(select(Entry).where(Entry.kind == "recall"))) == []


async def test_search_then_recall_then_follow_up(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(
        sessions,
        41,
        "Chicken thighs recipe",
        "Lemon-garlic thighs, 40 minutes at 200°C.",
        started=at(1),
        embedding=CHICKEN,
        entries=("chicken thighs tonight?", "40 minutes at 200°C"),
    )
    mem = memory(sessions, FakeEmbedder([CHICKEN]))
    model = FakeModel(
        [
            calls((SEARCH_CONVERSATIONS, '{"query": "chicken recipe"}')),
            calls((RECALL_CONVERSATION, '{"id": 41}')),
            text("That was #41, sir: lemon and garlic."),
            text("40 minutes, sir."),
        ]
    )
    turn = await owner_turn(sessions, "remember the chicken recipe?")
    result = await agent(sessions, model, memory=mem).run(turn)
    assert result.reply == "That was #41, sir: lemon and garlic."

    search_result = model.requests[1][0][-1]
    assert (
        search_result["role"] == "tool"
        and '#41 "Chicken thighs recipe"' in search_result["content"]
    )
    recall_result = model.requests[2][0][-1]["content"]
    assert recall_result.startswith('#41 "Chicken thighs recipe", Sun 1 Nov 2026')
    rows = await tool_rows(sessions)
    assert [(r.tool, r.arguments, r.ok) for r in rows] == [
        (SEARCH_CONVERSATIONS, {"query": "chicken recipe"}, True),
        (RECALL_CONVERSATION, {"id": 41}, True),
    ]

    # The recall stays in the conversation: the next turn still sees it.
    await History(sessions).append("assistant", result.reply, queue_item_id=turn.queue_item_id)
    await agent(sessions, model, memory=mem).run(
        await owner_turn(sessions, "and how long in the oven?")
    )
    contents = [m["content"] for m in model.requests[3][0][1:]]
    assert contents[0] == "remember the chicken recipe?"
    assert contents[1].startswith('[recall:#41 "Chicken thighs recipe"] #41 ')
    assert "Owner: 40 minutes at 200°C" in contents[1]
    assert contents[-1] == "and how long in the oven?"


async def test_memory_tool_errors_reach_the_model(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel(
        [calls((SEARCH_CONVERSATIONS, "{}"), (RECALL_CONVERSATION, '{"id": 9999}')), text("?")]
    )
    await agent(sessions, model, memory=memory(sessions, FakeEmbedder())).run(
        await owner_turn(sessions, "remember?")
    )
    results = [m["content"] for m in model.requests[1][0] if m["role"] == "tool"]
    assert results[0].startswith("Error: give a query")
    assert results[1] == "Error: there is no conversation #9999."
    assert [r.ok for r in await tool_rows(sessions)] == [False, False]


async def test_memory_tool_with_invalid_json(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([calls((RECALL_CONVERSATION, "{oops")), text("?")])
    await agent(sessions, model, memory=memory(sessions, FakeEmbedder())).run(
        await owner_turn(sessions, "remember 41")
    )
    assert "not valid JSON" in model.requests[1][0][-1]["content"]
