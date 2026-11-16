"""The expiry loop (change add-user-input-requests, design D5)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.conversations.store import History
from alfred.db.models import QueueItem
from alfred.inputs.loop import _expire_once, run_expiry_loop
from alfred.inputs.store import InputRequests
from alfred.kernel.access import _confirm_question
from alfred.kernel.users import User, Users
from tests.helpers import ANA, DIOGO, add_queue_item

USERS = Users((DIOGO, ANA))
QUESTION = _confirm_question("mcparr_add_media", {"title": "Dune"})


class Edits:
    def __init__(self) -> None:
        self.edits: list[tuple[str, int, str]] = []

    async def edit_question(self, user: User, message_id: int, html: str) -> None:
        self.edits.append((user.name, message_id, html))


async def waiting(
    sessions: async_sessionmaker[AsyncSession], expires_in: timedelta, message_id: int = 800
) -> tuple[InputRequests, int]:
    history = History(sessions)
    item = await add_queue_item(sessions, "user", "add Dune", user="ana")
    await history.append("ana", "user", "add Dune", queue_item_id=item)
    requests = InputRequests(sessions, history, USERS)
    request = await requests.park(
        item, "ana", tool="mcparr_add_media", arguments={"title": "Dune"}, question=QUESTION,
        state={"via": "confirm"}, turn={}, expires_at=expires_in,
    )  # fmt: skip
    await requests.set_message(request, "ana", message_id)
    return requests, item


async def test_nobody_answers(sessions: async_sessionmaker[AsyncSession]) -> None:
    requests, item = await waiting(sessions, timedelta(minutes=-1))
    outbox = Edits()
    ended = await _expire_once(requests, outbox, USERS)
    assert [r.queue_item_id for r in ended] == [item]
    async with sessions() as session:
        row = await session.get(QueueItem, item)
        assert row is not None and row.status == "cancelled"
    entries = await History(sessions).recent("ana", 30)
    assert entries[-1].kind == "assistant"
    assert entries[-1].text == notices.input_not_done("madam", "mcparr_add_media", "expired")
    ((user, message_id, html),) = outbox.edits
    assert (user, message_id) == ("ana", 800)
    assert html.endswith("<i>Expired: no answer in time.</i>")


async def test_open_requests_are_left_alone(sessions: async_sessionmaker[AsyncSession]) -> None:
    requests, item = await waiting(sessions, timedelta(hours=1))
    outbox = Edits()
    assert await _expire_once(requests, outbox, USERS) == []
    assert outbox.edits == []


async def test_loop_keeps_checking_after_a_failure(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    requests, _ = await waiting(sessions, timedelta(minutes=-1))
    calls = 0
    real = requests.expire_due

    async def flaky() -> list:  # type: ignore[type-arg]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("database away")
        return await real()

    requests.expire_due = flaky  # type: ignore[method-assign]
    outbox = Edits()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_expiry_loop(
            requests, outbox, USERS, 30,
            now=lambda: datetime(2026, 11, 15, 12, 0, 10, tzinfo=UTC), sleep=sleep,
        )  # fmt: skip
    assert sleeps == [20, 20]  # on the clock: the next :30
    assert len(outbox.edits) == 1
    assert "input request expiry check failed" in caplog.text
