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
    rid = await store.set("call mum", NOW + timedelta(hours=1), "diogo", ["diogo"])
    row = await store.get(rid)
    assert row is not None
    assert (row.text, row.due_at, row.status) == ("call mum", NOW + timedelta(hours=1), "pending")
    assert await store.get(rid + 100) is None


async def test_pending_soonest_first(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    later = await store.set("check the oven", NOW + timedelta(hours=2), "diogo", ["diogo"])
    sooner = await store.set("call mum", NOW + timedelta(hours=1), "diogo", ["diogo"])
    gone = await store.set("stretch", NOW + timedelta(minutes=5), "diogo", ["diogo"])
    await store.cancel(gone, NOW, "diogo")
    assert [r.id for r in await store.pending("diogo")] == [sooner, later]


async def test_cancel_pending(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    rid = await store.set("call mum", NOW + timedelta(hours=1), "diogo", ["diogo"])
    outcome, row = await store.cancel(rid, NOW, "diogo")
    assert outcome == "cancelled"
    assert row is not None and row.text == "call mum"
    assert await store.pending("diogo") == []
    # Never delivered.
    assert await store.claim_due(NOW + timedelta(days=1)) == []
    async with sessions() as session:
        reminder = await session.get(Reminder, rid)
        assert reminder is not None and reminder.cancelled_at == NOW


async def test_cancel_unknown_fired_or_cancelled(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = Reminders(sessions)
    assert await store.cancel(99, NOW, "diogo") == ("unknown", None)
    fired = await store.set("call mum", NOW - timedelta(days=1), "diogo", ["diogo"])
    await store.claim_due(NOW)
    outcome, _ = await store.cancel(fired, NOW, "diogo")
    assert outcome == "fired"
    cancelled = await store.set("stretch", NOW + timedelta(hours=1), "diogo", ["diogo"])
    await store.cancel(cancelled, NOW, "diogo")
    outcome, _ = await store.cancel(cancelled, NOW, "diogo")
    assert outcome == "cancelled_before"


async def test_claim_due_in_due_order(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    second = await store.set("second", NOW - timedelta(minutes=5), "diogo", ["diogo"])
    future = await store.set("future", NOW + timedelta(minutes=5), "diogo", ["diogo"])
    first = await store.set("first", NOW - timedelta(days=3), "diogo", ["diogo"])
    exactly_now = await store.set("now", NOW, "diogo", ["diogo"])

    ids = await store.claim_due(NOW)
    assert len(ids) == 3
    items = await queue_items(sessions)
    assert [(i.kind, i.reminder_id, i.text, i.status) for i in items] == [
        ("reminder", first, "first", "pending"),
        ("reminder", second, "second", "pending"),
        ("reminder", exactly_now, "now", "pending"),
    ]
    assert [r.id for r in await store.pending("diogo")] == [future]
    row = await store.get(first)
    assert row is not None and row.status == "fired"


async def test_claimed_reminder_not_claimed_twice(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = Reminders(sessions)
    await store.set("call mum", NOW, "diogo", ["diogo"])
    assert len(await store.claim_due(NOW)) == 1
    assert await store.claim_due(NOW + timedelta(minutes=1)) == []
    assert len(await queue_items(sessions)) == 1


async def test_nothing_due(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    await store.set("call mum", NOW + timedelta(hours=1), "diogo", ["diogo"])
    assert await store.claim_due(NOW) == []
    assert await queue_items(sessions) == []


# --- Creator and recipients (change add-multiple-users) ------------------------------


async def test_creator_and_recipients_stored(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    rid = await store.set("dinner", NOW + timedelta(hours=1), "ana", ["diogo", "ana", "diogo"])
    row = await store.get(rid)
    assert row is not None
    assert (row.created_by, row.recipients) == ("ana", ("ana", "diogo"))
    assert not row.only_for("ana")
    own = await store.get(await store.set("x", NOW + timedelta(hours=1), "ana", ["ana"]))
    assert own is not None and own.only_for("ana")


async def test_listing_by_involvement(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    for_ana = await store.set("buy milk", NOW + timedelta(hours=1), "diogo", ["ana"])
    shared = await store.set("dinner", NOW + timedelta(hours=2), "diogo", ["diogo", "ana"])
    hers = await store.set("yoga", NOW + timedelta(hours=3), "ana", ["ana"])
    assert [r.id for r in await store.pending("ana")] == [for_ana, shared, hers]
    # diogo set #for_ana, so he sees it; ana's own reminder is not his business.
    assert [r.id for r in await store.pending("diogo")] == [for_ana, shared]
    assert await store.pending("bob") == []


async def test_a_recipient_cancels_for_everyone(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = Reminders(sessions)
    shared = await store.set("dinner", NOW + timedelta(hours=2), "diogo", ["diogo", "ana"])
    outcome, row = await store.cancel(shared, NOW, "ana")
    assert outcome == "cancelled" and row is not None and row.recipients == ("ana", "diogo")
    assert await store.pending("diogo") == [] and await store.pending("ana") == []
    assert await store.claim_due(NOW + timedelta(days=1)) == []


async def test_not_involved_is_unknown(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions)
    hers = await store.set("yoga", NOW + timedelta(hours=3), "ana", ["ana"])
    assert await store.cancel(hers, NOW, "diogo") == ("unknown", None)
    fired = await store.set("call mum", NOW - timedelta(days=1), "ana", ["ana"])
    await store.claim_due(NOW)
    assert await store.cancel(fired, NOW, "diogo") == ("unknown", None)
    assert [r.id for r in await store.pending("ana")] == [hers]


async def test_shared_reminder_queues_one_item_per_recipient(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = Reminders(sessions)
    early = await store.set("early", NOW - timedelta(hours=1), "ana", ["ana"])
    shared = await store.set("dinner", NOW, "ana", ["diogo", "ana"])
    ids = await store.claim_due(NOW)
    items = await queue_items(sessions)
    assert [i.id for i in items] == ids
    assert [(i.kind, i.reminder_id, i.user_name) for i in items] == [
        ("reminder", early, "ana"),
        ("reminder", shared, "ana"),
        ("reminder", shared, "diogo"),
    ]
    # Exactly once: claimed again later, nothing more is queued.
    assert await store.claim_due(NOW + timedelta(minutes=1)) == []
    assert len(await queue_items(sessions)) == 3


async def test_a_reminder_needs_a_recipient(sessions: async_sessionmaker[AsyncSession]) -> None:
    import pytest

    with pytest.raises(ValueError, match="recipient"):
        await Reminders(sessions).set("x", NOW, "diogo", [])
