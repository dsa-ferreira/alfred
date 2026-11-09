from __future__ import annotations

from alfred.agent.conversation import END_CONVERSATION, ConversationTools
from alfred.agent.toolset import AttemptContext
from alfred.agent.turn import TurnInput

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
