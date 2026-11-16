from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.features.reminders.store import Reminders
from alfred.runtime.turns.due_reminders import _claim_once
from alfred.runtime.turns.intake import ChatIntake
from alfred.runtime.turns.queue import Queue


class Ticks:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_arrivals_are_reported(sessions: async_sessionmaker[AsyncSession]) -> None:
    queue = Queue(sessions)
    activity = queue.activity
    assert not activity.arrived.is_set()
    assert not await queue.has_pending()

    await ChatIntake(sessions, queue).enqueue_user("diogo", "hi")
    assert activity.arrived.is_set() and queue.signal.is_set()
    assert await queue.has_pending()

    activity.arrived.clear()
    await queue.enqueue_ask("backup", "anything to report?", "diogo")
    assert activity.arrived.is_set()

    activity.arrived.clear()
    reminders = Reminders(sessions)
    await reminders.set("call mum", datetime.now(UTC) - timedelta(minutes=1), "diogo", ["diogo"])
    await _claim_once(sessions, reminders, queue, datetime.now(UTC))
    assert activity.arrived.is_set()


async def test_quiet_period_starts_at_construction(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    ticks = Ticks()
    queue = Queue(sessions, clock=ticks)
    assert queue.activity.quiet_for() == 0
    ticks.now += 7
    assert queue.activity.quiet_for() == 7
