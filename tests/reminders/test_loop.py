from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.models import QueueItem
from alfred.reminders.loop import run_due_loop, seconds_to_next_check
from alfred.reminders.store import Reminders

DUE = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)


class Stop(Exception):
    pass


class FakeClock:
    """``now`` for the loop; its ``sleep`` advances the clock and stops after ``passes``."""

    def __init__(self, start: datetime, passes: int) -> None:
        self.now = start
        self.passes = passes
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) >= self.passes:
            raise Stop
        self.now += timedelta(seconds=seconds)


async def run(store: Reminders, signal: asyncio.Event, clock: FakeClock) -> None:
    with pytest.raises(Stop):
        await run_due_loop(store, signal, 30, now=clock, sleep=clock.sleep)


async def queue_items(sessions: async_sessionmaker[AsyncSession]) -> list[QueueItem]:
    async with sessions() as session:
        return list(await session.scalars(select(QueueItem).order_by(QueueItem.id)))


async def test_reminder_comes_due(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    rid = await store.set("call mum", DUE)
    signal = asyncio.Event()
    # Started at 16:59:20: checks at 16:59:20, then on the clock at 16:59:30 and 17:00:00.
    clock = FakeClock(DUE - timedelta(seconds=40), passes=3)
    await run(store, signal, clock)
    assert clock.sleeps == [10, 30, 30]
    [item] = await queue_items(sessions)
    row = await store.get(rid)
    assert row is not None
    async with sessions() as session:
        from alfred.db.models import Reminder

        fired = await session.get(Reminder, rid)
        assert fired is not None and fired.fired_at == DUE  # queued at 17:00:00 exactly
    assert (item.kind, item.reminder_id, item.status) == ("reminder", rid, "pending")
    assert item.created_at is not None
    assert signal.is_set()
    assert await store.pending() == []


async def test_nothing_due(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    await store.set("call mum", DUE + timedelta(days=2))
    signal = asyncio.Event()
    # A whole day of checks every 30 seconds.
    clock = FakeClock(DUE, passes=24 * 120)
    await run(store, signal, clock)
    # The worker is never woken and has nothing to send to the AI.
    assert not signal.is_set()
    assert await queue_items(sessions) == []


async def test_alfred_down_at_the_due_time(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    rid = await store.set("call mum", DUE)
    signal = asyncio.Event()
    # alfred starts at 19:40: the first pass, before any sleep, queues it.
    clock = FakeClock(DUE + timedelta(hours=2, minutes=40), passes=1)
    await run(store, signal, clock)
    [item] = await queue_items(sessions)
    assert item.reminder_id == rid
    assert signal.is_set()


async def test_days_late(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    later = await store.set("water the plants", DUE - timedelta(days=1))
    first = await store.set("call mum", DUE - timedelta(days=3))
    signal = asyncio.Event()
    await run(store, signal, FakeClock(DUE, passes=1))
    assert [i.reminder_id for i in await queue_items(sessions)] == [first, later]


async def test_failure_does_not_stop_the_loop() -> None:
    class Broken:
        calls = 0

        async def claim_due(self, now: datetime) -> list[int]:
            self.calls += 1
            raise OSError("database down")

    broken = Broken()
    clock = FakeClock(DUE, passes=3)
    with pytest.raises(Stop):
        await run_due_loop(broken, asyncio.Event(), 30, now=clock, sleep=clock.sleep)  # type: ignore[arg-type]
    assert broken.calls == 3


@pytest.mark.parametrize(
    ("second", "interval", "expected"),
    [
        (20.0, 30, 10),  # to :30
        (30.0, 30, 30),  # just checked at :30, to :00
        (59.9995, 30, 0.0005),  # woke a hair early: check again at :00
        (0.0, 30, 30),
        (17.0, 45, 28),  # to :45
        (50.0, 45, 10),  # to :00, never skipping the minute
        (5.0, 60, 55),
        (12.5, 10, 7.5),
    ],
)
def test_seconds_to_next_check(second: float, interval: float, expected: float) -> None:
    now = DUE + timedelta(seconds=second)
    assert seconds_to_next_check(now, interval) == pytest.approx(expected)
