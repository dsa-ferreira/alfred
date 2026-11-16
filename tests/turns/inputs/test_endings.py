"""Recording turns whose questions ended unanswered, through their kinds (design D5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.messages import to_messages
from alfred.conversations.store import History
from alfred.kernel.questions import Option, Question
from alfred.kernel.users import Users
from alfred.turns.inputs import notices
from alfred.turns.inputs.endings import Endings
from alfred.turns.inputs.store import InputRequests
from tests.helpers import ANA, DIOGO, add_queue_item, make_endings

QUESTION = Question("Run mcparr_add_media?", (Option("Do it", True), Option("Cancel", False)))
LATER = datetime.now(UTC) + timedelta(hours=1)
EARLIER = datetime.now(UTC) - timedelta(minutes=1)
USERS = Users((DIOGO, ANA))


def endings(sessions: async_sessionmaker[AsyncSession]) -> Endings:
    return make_endings(sessions, USERS)


async def parked(
    sessions: async_sessionmaker[AsyncSession],
    user: str = "ana",
    kind: str = "chat",
    *,
    expires_at: datetime = LATER,
    text_: str = "add Dune",
    source: str | None = None,
    due_at: datetime | None = None,
) -> tuple[int, int]:
    """A parked item of ``kind`` for ``user`` with its open request: (item id, request id)."""
    item = await add_queue_item(sessions, kind, text_, source=source, user=user, due_at=due_at)
    request = await InputRequests(sessions).park(
        item, user, tool="mcparr_add_media", arguments={"title": "Dune"}, question=QUESTION,
        state={}, turn={}, expires_at=expires_at,
    )  # fmt: skip
    return item, request


async def test_chat_ending_is_an_assistant_entry(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    item, _ = await parked(sessions)
    await history.append("ana", "user", "add Dune", turn_id=item)
    await endings(sessions).record(await InputRequests(sessions).cancel_for("ana", "new_message"))
    entries = await history.recent("ana", 30)
    assert [(e.kind, e.text) for e in entries] == [
        ("user", "add Dune"),
        ("assistant", notices.input_not_done("madam", "mcparr_add_media", "new_message")),
    ]
    # Later turn knows: the next turn's context carries it as alfred's reply.
    messages = to_messages(entries)
    assert messages[-1] == {
        "role": "assistant",
        "content": notices.input_not_done("madam", "mcparr_add_media", "new_message"),
    }


async def test_ask_ending_records_the_prompt_and_the_reply(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, request = await parked(sessions, "diogo", "ask", text_="anything to add?", source="jellyfin")
    ended = await InputRequests(sessions).cancel(request, "diogo", "not_delivered")
    await endings(sessions).record(ended)
    entries = await History(sessions).recent("diogo", 30)
    assert [(e.kind, e.source, e.text) for e in entries] == [
        ("ask_prompt", "jellyfin", "anything to add?"),
        (
            "ask_reply",
            "jellyfin",
            notices.input_not_done("sir", "mcparr_add_media", "not_delivered"),
        ),
    ]


async def test_reminder_ending_is_its_delivered_message(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    due = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)
    await parked(sessions, "diogo", "reminder", text_="call mum", expires_at=EARLIER, due_at=due)
    await endings(sessions).record(await InputRequests(sessions).expire_due())
    entries = await History(sessions).recent("diogo", 30)
    assert [(e.kind, e.text, e.reminder_due_at) for e in entries] == [
        ("reminder", "call mum", due),
        ("reminder_reply", notices.input_not_done("sir", "mcparr_add_media", "expired"), due),
    ]


async def test_late_tap_ending_is_recorded_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, request = await parked(sessions, expires_at=EARLIER)
    result = await InputRequests(sessions).answer(request, 0, "ana")
    assert result.expired and result.request is not None
    await endings(sessions).record([result.request])
    # A second tap finds it ended and returns nothing to record.
    again = await InputRequests(sessions).answer(request, 0, "ana")
    assert not again.expired
    entries = await History(sessions).recent("ana", 30)
    assert [e.text for e in entries] == [
        notices.input_not_done("madam", "mcparr_add_media", "expired")
    ]
