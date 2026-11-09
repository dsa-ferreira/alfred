from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.models import QueueItem, Reminder
from alfred.reminders.store import Reminders

NOW = datetime(2026, 11, 9, 16, 47, tzinfo=UTC)


async def queue_items(sessions: async_sessionmaker[AsyncSession]) -> list[QueueItem]:
    async with sessions() as session:
        return list(await session.scalars(select(QueueItem).order_by(QueueItem.id)))


async def test_set_and_get(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    rid = await store.set("call mum", NOW + timedelta(hours=1))
    row = await store.get(rid)
    assert row is not None
    assert (row.text, row.due_at, row.status) == ("call mum", NOW + timedelta(hours=1), "pending")
    assert await store.get(rid + 100) is None


async def test_pending_soonest_first(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    later = await store.set("check the oven", NOW + timedelta(hours=2))
    sooner = await store.set("call mum", NOW + timedelta(hours=1))
    gone = await store.set("stretch", NOW + timedelta(minutes=5))
    await store.cancel(gone, NOW)
    assert [r.id for r in await store.pending()] == [sooner, later]


async def test_cancel_pending(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    rid = await store.set("call mum", NOW + timedelta(hours=1))
    outcome, row = await store.cancel(rid, NOW)
    assert outcome == "cancelled"
    assert row is not None and row.text == "call mum"
    assert await store.pending() == []
    # Never delivered.
    assert await store.claim_due(NOW + timedelta(days=1)) == []
    async with sessions() as session:
        reminder = await session.get(Reminder, rid)
        assert reminder is not None and reminder.cancelled_at == NOW


async def test_cancel_unknown_fired_or_cancelled(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = Reminders(sessions)
    assert await store.cancel(99, NOW) == ("unknown", None)
    fired = await store.set("call mum", NOW - timedelta(days=1))
    await store.claim_due(NOW)
    outcome, _ = await store.cancel(fired, NOW)
    assert outcome == "fired"
    cancelled = await store.set("stretch", NOW + timedelta(hours=1))
    await store.cancel(cancelled, NOW)
    outcome, _ = await store.cancel(cancelled, NOW)
    assert outcome == "cancelled_before"


async def test_claim_due_in_due_order(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    second = await store.set("second", NOW - timedelta(minutes=5))
    future = await store.set("future", NOW + timedelta(minutes=5))
    first = await store.set("first", NOW - timedelta(days=3))
    exactly_now = await store.set("now", NOW)

    ids = await store.claim_due(NOW)
    assert len(ids) == 3
    items = await queue_items(sessions)
    assert [(i.kind, i.reminder_id, i.text, i.status) for i in items] == [
        ("reminder", first, "first", "pending"),
        ("reminder", second, "second", "pending"),
        ("reminder", exactly_now, "now", "pending"),
    ]
    assert [r.id for r in await store.pending()] == [future]
    row = await store.get(first)
    assert row is not None and row.status == "fired"


async def test_claimed_reminder_not_claimed_twice(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = Reminders(sessions)
    await store.set("call mum", NOW)
    assert len(await store.claim_due(NOW)) == 1
    assert await store.claim_due(NOW + timedelta(minutes=1)) == []
    assert len(await queue_items(sessions)) == 1


async def test_nothing_due(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    await store.set("call mum", NOW + timedelta(hours=1))
    assert await store.claim_due(NOW) == []
    assert await queue_items(sessions) == []
