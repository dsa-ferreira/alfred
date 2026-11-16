from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.attachments import NewAttachment
from alfred.attachments.tables import Attachment
from alfred.reminders.store import Reminders
from alfred.turns.queue import Queue
from alfred.turns.tables import QueueItem


class Ticks:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_items_carry_their_user(sessions: async_sessionmaker[AsyncSession]) -> None:
    queue = Queue(sessions)
    photo = NewAttachment("image", "image/jpeg", 1000, "attachments/a", width=10, height=10)
    await queue.enqueue_user("ana", "what's this?", 5, [photo])
    await queue.enqueue_ask("mcparr", "Dune finished", "ana")
    async with sessions() as session:
        items = list(await session.scalars(select(QueueItem).order_by(QueueItem.id)))
        (attachment,) = await session.scalars(select(Attachment))
    assert [(i.kind, i.user_name, i.source) for i in items] == [
        ("user", "ana", None),
        ("ask", "ana", "mcparr"),
    ]
    assert attachment.user_name == "ana"


async def test_arrivals_are_reported(sessions: async_sessionmaker[AsyncSession]) -> None:
    from alfred.reminders.loop import _claim_once

    queue = Queue(sessions)
    activity = queue.activity
    assert not activity.arrived.is_set()
    assert not await queue.has_pending()

    await queue.enqueue_user("diogo", "hi")
    assert activity.arrived.is_set() and queue.signal.is_set()
    assert await queue.has_pending()

    activity.arrived.clear()
    await queue.enqueue_ask("backup", "anything to report?", "diogo")
    assert activity.arrived.is_set()

    activity.arrived.clear()
    reminders = Reminders(sessions)
    await reminders.set("call mum", datetime.now(UTC) - timedelta(minutes=1), "diogo", ["diogo"])
    await _claim_once(reminders, activity.notify, lambda: datetime.now(UTC))
    assert activity.arrived.is_set()


async def test_quiet_period_starts_at_construction(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    ticks = Ticks()
    queue = Queue(sessions, clock=ticks)
    assert queue.activity.quiet_for() == 0
    ticks.now += 7
    assert queue.activity.quiet_for() == 7
