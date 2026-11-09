from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.prompts import NO_REPLY
from alfred.history import History
from alfred.tools.base import AttemptContext, TurnInput
from alfred.tools.conversation import END_CONVERSATION, ConversationTools
from tests.agent.turn_helpers import agent, owner_turn
from tests.helpers import FakeModel, add_queue_item, calls, text

OWNER = TurnInput("owner", "thanks, that's all", 1)


async def test_offered_on_owner_turns_only() -> None:
    tools = ConversationTools()
    assert await tools.open(OWNER) is tools
    assert await tools.open(TurnInput("ask", "x", 2, "svc")) is None
    assert await tools.open(TurnInput("reminder", "x", 3)) is None


async def test_call_requests_a_reset() -> None:
    ctx = AttemptContext(OWNER)
    outcome = await ConversationTools().call(END_CONVERSATION, "{}", ctx)
    assert ctx.reset
    assert outcome.ok
    assert outcome.text == (
        "The conversation will be archived after your reply. Reply with a short closing line."
    )


# --- alfred_end_conversation ---------------------------------------------------


async def test_end_conversation_offered_on_owner_turns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model).run(await owner_turn(sessions, "hi"))
    assert END_CONVERSATION in model.tool_names()
    assert "mcparr_get_downloads" in model.tool_names()
    description = next(
        t["function"]["description"]
        for t in model.requests[0][1]
        if t["function"]["name"] == END_CONVERSATION
    )
    assert "ONLY" in description


async def test_end_conversation_not_offered_on_ask_turns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    item = await add_queue_item(sessions, "ask", "x", "disk-check")
    model = FakeModel([calls((END_CONVERSATION, "{}")), text(NO_REPLY)])
    result = await agent(sessions, model).run(TurnInput("ask", "x", item, "disk-check"))
    assert END_CONVERSATION not in model.tool_names()
    assert not result.reset_requested
    assert "unknown tool" in model.requests[1][0][-1]["content"]


async def test_end_conversation_marks_reset(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([calls((END_CONVERSATION, "{}")), text("Done 👍")])
    result = await agent(sessions, model).run(await owner_turn(sessions, "we're done here"))
    assert result.reply == "Done 👍"
    assert result.reset_requested
    # The reset itself happens after delivery, not during the turn.
    assert [e.text for e in await History(sessions).recent(30)] == ["we're done here"]
