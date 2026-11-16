"""The input request store: transitions, at-most-once, and endings (design D4, D5)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.agent.messages import to_messages
from alfred.db.models import InputRequest, QueueItem
from alfred.history import History
from alfred.inputs.store import InputRequests
from alfred.kernel.questions import Option, Question
from alfred.kernel.users import Users
from tests.helpers import ANA, DIOGO, add_queue_item

QUESTION = Question(
    "Run mcparr_add_media with these arguments?",
    (Option("Do it", True), Option("Cancel", False)),
    detail='{"title": "Dune"}',
    title="For your request:",
)
LATER = datetime.now(UTC) + timedelta(hours=1)
EARLIER = datetime.now(UTC) - timedelta(minutes=1)


def store(sessions: async_sessionmaker[AsyncSession]) -> InputRequests:
    return InputRequests(sessions, History(sessions), Users((DIOGO, ANA)))


async def parked(
    sessions: async_sessionmaker[AsyncSession],
    user: str = "ana",
    kind: str = "user",
    *,
    expires_at: datetime = LATER,
    text_: str = "add Dune",
    source: str | None = None,
) -> tuple[int, int]:
    """A parked item of ``kind`` for ``user`` with its open request: (item id, request id)."""
    item = await add_queue_item(sessions, kind, text_, source=source, user=user)
    request = await store(sessions).park(
        item,
        user,
        tool="mcparr_add_media",
        arguments={"title": "Dune"},
        question=QUESTION,
        state={"via": "confirm"},
        turn={"tail": []},
        expires_at=expires_at,
    )
    return item, request


async def statuses(
    sessions: async_sessionmaker[AsyncSession], item: int, request: int
) -> tuple[str, str]:
    async with sessions() as session:
        return (
            (await session.get(QueueItem, item)).status,  # type: ignore[union-attr]
            (await session.get(InputRequest, request)).status,  # type: ignore[union-attr]
        )


async def test_park_makes_the_item_wait(sessions: async_sessionmaker[AsyncSession]) -> None:
    item, request = await parked(sessions)
    assert await statuses(sessions, item, request) == ("waiting", "open")
    (row,) = await store(sessions)._open_for("ana")
    assert row.question == QUESTION and row.tool == "mcparr_add_media"
    await store(sessions).set_message(request, "ana", 991)
    assert (await store(sessions)._open_for("ana"))[0].telegram_message_id == 991


async def test_answer_wakes_the_item(sessions: async_sessionmaker[AsyncSession]) -> None:
    item, request = await parked(sessions)
    result = await store(sessions).answer(request, 0, "ana")
    assert (result.outcome, result.label) == ("answered", "Do it")
    assert result.request is not None and result.request.answer is True
    assert await statuses(sessions, item, request) == ("pending", "answered")


async def test_two_taps_at_once_answer_once(sessions: async_sessionmaker[AsyncSession]) -> None:
    _, request = await parked(sessions)
    results = await asyncio.gather(
        store(sessions).answer(request, 0, "ana"), store(sessions).answer(request, 1, "ana")
    )
    assert sorted(r.outcome for r in results) == ["answered", "not_open"]
    async with sessions() as session:
        row = await session.get(InputRequest, request)
    winner = next(r for r in results if r.outcome == "answered")
    assert row is not None and row.answer == (winner.label == "Do it")


async def test_another_user_cant_answer(sessions: async_sessionmaker[AsyncSession]) -> None:
    item, request = await parked(sessions)
    assert (await store(sessions).answer(request, 0, "diogo")).outcome == "not_yours"
    assert await statuses(sessions, item, request) == ("waiting", "open")


async def test_unknown_request_or_option(sessions: async_sessionmaker[AsyncSession]) -> None:
    item, request = await parked(sessions)
    assert (await store(sessions).answer(request + 99, 0, "ana")).outcome == "not_open"
    assert (await store(sessions).answer(request, 5, "ana")).outcome == "not_open"
    assert await statuses(sessions, item, request) == ("waiting", "open")


async def test_late_tap_expires_the_request(sessions: async_sessionmaker[AsyncSession]) -> None:
    item, request = await parked(sessions, expires_at=EARLIER)
    result = await store(sessions).answer(request, 0, "ana")
    assert result.outcome == "not_open" and result.expired
    assert result.request is not None and result.request.cancel_reason == "expired"
    assert await statuses(sessions, item, request) == ("cancelled", "expired")
    # Its ending is recorded, once.
    entries = await History(sessions).recent("ana", 30)
    assert [e.text for e in entries] == [
        notices.input_not_done("madam", "mcparr_add_media", "expired")
    ]


async def test_cancel_for_ends_only_that_users_open_requests(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    ana_open = await parked(sessions)
    ana_answered = await parked(sessions)
    await store(sessions).answer(ana_answered[1], 0, "ana")
    diogo_open = await parked(sessions, "diogo")
    ended = await store(sessions).cancel_for("ana", "new_message")
    assert [r.id for r in ended] == [ana_open[1]]
    assert ended[0].cancel_reason == "new_message"
    assert await statuses(sessions, *ana_open) == ("cancelled", "cancelled")
    assert await statuses(sessions, *ana_answered) == ("pending", "answered")
    assert await statuses(sessions, *diogo_open) == ("waiting", "open")


async def test_expire_due_ends_only_expired_ones(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    old = await parked(sessions, expires_at=EARLIER)
    fresh = await parked(sessions, "diogo")
    ended = await store(sessions).expire_due()
    assert [r.id for r in ended] == [old[1]]
    assert await statuses(sessions, *old) == ("cancelled", "expired")
    assert await statuses(sessions, *fresh) == ("waiting", "open")
    assert await store(sessions).expire_due() == []


async def test_running_and_finish(sessions: async_sessionmaker[AsyncSession]) -> None:
    item, request = await parked(sessions)
    requests = store(sessions)
    await requests.answer(request, 0, "ana")
    # Every query names the user: another user's item has nothing for them.
    assert await requests.take_answered(item, "diogo") is None
    taken = await requests.take_answered(item, "ana")
    assert taken is not None and taken.answer is True and taken.turn == {"tail": []}
    assert await requests.take_answered(item, "ana") is None  # only once
    assert await requests.running_for(item, "diogo") is None
    assert (await requests.running_for(item, "ana")) is not None
    await requests.finish(request, "diogo")
    assert (await statuses(sessions, item, request))[1] == "running"
    await requests.finish(request, "ana")
    assert (await statuses(sessions, item, request))[1] == "done"
    assert await requests.running_for(item, "ana") is None


async def test_set_message_names_the_user(sessions: async_sessionmaker[AsyncSession]) -> None:
    _, request = await parked(sessions)
    await store(sessions).set_message(request, "diogo", 5)
    assert (await store(sessions)._open_for("ana"))[0].telegram_message_id is None


async def test_one_live_request_per_item_after_an_answer(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A resumed turn that parks again gets a new request once the old one is done."""
    item, request = await parked(sessions)
    requests = store(sessions)
    await requests.answer(request, 0, "ana")
    await requests.take_answered(item, "ana")
    await requests.finish(request, "ana")
    async with sessions.begin() as session:
        await session.execute(
            update(QueueItem).where(QueueItem.id == item).values(status="pending")
        )
    again = await requests.park(
        item, "ana", tool="t", arguments={}, question=QUESTION, state={}, turn={},
        expires_at=LATER,
    )  # fmt: skip
    assert await statuses(sessions, item, again) == ("waiting", "open")


# --- the ending path (design D5) -------------------------------------------------


async def test_chat_ending_is_an_assistant_entry(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    item, _ = await parked(sessions)
    await history.append("ana", "user", "add Dune", queue_item_id=item)
    await store(sessions).cancel_for("ana", "new_message")
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
    await store(sessions).cancel(request, "diogo", "not_delivered")
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
    item, _ = await parked(sessions, "diogo", "reminder", text_="call mum", expires_at=EARLIER)
    due = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)
    async with sessions.begin() as session:
        await session.execute(
            update(QueueItem).where(QueueItem.id == item).values(reminder_due_at=due)
        )
    await store(sessions).expire_due()
    entries = await History(sessions).recent("diogo", 30)
    assert [(e.kind, e.text, e.reminder_due_at) for e in entries] == [
        ("reminder", "call mum", due),
        ("reminder_reply", notices.input_not_done("sir", "mcparr_add_media", "expired"), due),
    ]


async def test_queries_filter_on_the_user(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Nothing in one user's calls touches another user's rows."""
    ana = await parked(sessions)
    requests = store(sessions)
    await requests.cancel(ana[1], "diogo", "new_message")
    assert await requests.cancel_for("diogo") == []
    assert await statuses(sessions, *ana) == ("waiting", "open")
    async with sessions() as session:
        assert (await session.scalar(select(InputRequest.user_name))) == "ana"
