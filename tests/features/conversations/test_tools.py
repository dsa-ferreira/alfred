from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.features.conversations.store import History
from alfred.features.conversations.tools import _END_CONVERSATION, ConversationTools
from alfred.kernel.toolset import AttemptContext
from alfred.kernel.turn import AskTurn, ChatTurn, ReminderTurn
from alfred.runtime.agent.prompts import _NO_REPLY
from tests.helpers import DIOGO, FakeModel, add_queue_item, calls, text
from tests.runtime.agent.turn_helpers import agent, owner_turn

OWNER = ChatTurn("thanks, that's all", 1, user=DIOGO)


async def test_offered_on_owner_turns_only() -> None:
    tools = ConversationTools()
    assert await tools.open(OWNER) is tools
    assert await tools.open(AskTurn("x", 2, "svc", user=DIOGO)) is None
    assert await tools.open(ReminderTurn("x", 3, user=DIOGO)) is None


async def test_call_requests_a_reset() -> None:
    ctx = AttemptContext(OWNER)
    outcome = await ConversationTools().call(_END_CONVERSATION, "{}", ctx)
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
    assert _END_CONVERSATION in model.tool_names()
    assert "mcparr_get_downloads" in model.tool_names()
    description = next(
        t["function"]["description"]
        for t in model.requests[0][1]
        if t["function"]["name"] == _END_CONVERSATION
    )
    assert "ONLY" in description


async def test_end_conversation_not_offered_on_ask_turns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    item = await add_queue_item(sessions, "ask", "x", "disk-check")
    model = FakeModel([calls((_END_CONVERSATION, "{}")), text(_NO_REPLY)])
    result = await agent(sessions, model).run(AskTurn("x", item, "disk-check", user=DIOGO))
    assert _END_CONVERSATION not in model.tool_names()
    assert not result.reset_requested
    assert "unknown tool" in model.requests[1][0][-1]["content"]


async def test_end_conversation_marks_reset(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([calls((_END_CONVERSATION, "{}")), text("Done 👍")])
    result = await agent(sessions, model).run(await owner_turn(sessions, "we're done here"))
    assert result.reply == "Done 👍"
    assert result.reset_requested
    # The reset itself happens after delivery, not during the turn.
    assert [e.text for e in await History(sessions).recent("diogo", 30)] == ["we're done here"]
