from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.reminders.store import Reminders
from alfred.reminders.tables import Reminder
from alfred.reminders.when import parse_rule
from alfred.turns.tables import QueueItem

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


async def test_a_recipient_leaves_a_shared_reminder(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = Reminders(sessions)
    shared = await store.set("dinner", NOW + timedelta(hours=2), "diogo", ["diogo", "ana"])
    outcome, row = await store.cancel(shared, NOW, "ana")
    assert outcome == "left" and row is not None and row.recipients == ("diogo",)
    assert row.status == "pending"
    assert await store.pending("ana") == []
    assert [r.id for r in await store.pending("diogo")] == [shared]
    await store.claim_due(NOW + timedelta(days=1))
    assert [i.user_name for i in await queue_items(sessions)] == ["diogo"]


async def test_the_creator_cancels_for_everyone(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = Reminders(sessions)
    shared = await store.set("dinner", NOW + timedelta(hours=2), "diogo", ["diogo", "ana"])
    outcome, row = await store.cancel(shared, NOW, "diogo")
    assert outcome == "cancelled" and row is not None and row.recipients == ("ana", "diogo")
    assert await store.pending("diogo") == [] and await store.pending("ana") == []
    assert await store.claim_due(NOW + timedelta(days=1)) == []


async def test_the_last_recipient_leaving_cancels(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = Reminders(sessions)
    for_ana = await store.set("buy milk", NOW + timedelta(hours=1), "diogo", ["ana"])
    outcome, row = await store.cancel(for_ana, NOW, "ana")
    assert outcome == "cancelled" and row is not None and row.recipients == ()
    assert await store.pending("diogo") == [] and await store.pending("ana") == []
    assert await store.claim_due(NOW + timedelta(days=1)) == []
    reloaded = await store.get(for_ana)
    assert reloaded is not None and reloaded.status == "cancelled"


async def test_leaving_keeps_an_item_already_queued(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = Reminders(sessions)
    rule = parse_rule(repeat="daily", at="16:00")
    assert rule is not None
    shared = await store.set(
        "dinner", NOW - timedelta(minutes=47), "diogo", ["diogo", "ana"], rule=rule
    )
    await store.claim_due(NOW)
    outcome, _ = await store.cancel(shared, NOW, "ana")
    assert outcome == "left"
    items = await queue_items(sessions)
    assert [(i.user_name, i.status) for i in items] == [("ana", "pending"), ("diogo", "pending")]


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


# --- Repeating reminders (change add-recurring-reminders) ------------------------------

LISBON = ZoneInfo("Europe/Lisbon")


def lisbon(*args: int) -> datetime:
    return datetime(*args, tzinfo=LISBON)  # type: ignore[misc]


async def weekday_briefing(store: Reminders, due: datetime, recipients: list[str]) -> int:
    rule = parse_rule(repeat="weekdays", at="08:00")
    return await store.set("brief me", due, "diogo", recipients, rule=rule, quiet_ok=True)


async def test_set_stores_the_rule(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions, LISBON)
    rid = await weekday_briefing(store, lisbon(2026, 11, 16, 8, 0), ["diogo"])
    row = await store.get(rid)
    assert row is not None and row.rule is not None
    assert (row.rule.repeat, row.rule.days, row.rule.clock_text) == ("weekdays", 31, "08:00")
    assert row.quiet_ok
    one_shot = await store.get(await store.set("x", NOW, "diogo", ["diogo"]))
    assert one_shot is not None and one_shot.rule is None and not one_shot.quiet_ok


async def test_repeating_reminder_moves_on(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions, LISBON)
    friday = lisbon(2026, 11, 20, 8, 0)
    rid = await weekday_briefing(store, friday, ["diogo", "ana"])
    ids = await store.claim_due(friday)
    items = await queue_items(sessions)
    assert [i.id for i in items] == ids
    assert [(i.user_name, i.reminder_id, i.reminder_due_at) for i in items] == [
        ("ana", rid, friday),
        ("diogo", rid, friday),
    ]
    row = await store.get(rid)
    assert row is not None
    assert (row.status, row.due_at) == ("pending", lisbon(2026, 11, 23, 8, 0))
    # Still listed, by its next time.
    assert [(r.id, r.due_at) for r in await store.pending("diogo")] == [(rid, row.due_at)]
    # Not claimed again before then.
    assert await store.claim_due(friday + timedelta(hours=1)) == []


async def test_repeating_reminder_after_an_outage(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = Reminders(sessions, LISBON)
    rid = await weekday_briefing(store, lisbon(2026, 11, 16, 8, 0), ["diogo"])
    await store.claim_due(lisbon(2026, 11, 18, 10, 0))
    items = await queue_items(sessions)
    assert [i.reminder_due_at for i in items] == [lisbon(2026, 11, 18, 8, 0)]
    row = await store.get(rid)
    assert row is not None and row.due_at == lisbon(2026, 11, 19, 8, 0)


async def test_one_shot_items_carry_their_due_time(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = Reminders(sessions, LISBON)
    due = NOW - timedelta(hours=2)
    rid = await store.set("call mum", due, "diogo", ["diogo"])
    await store.claim_due(NOW)
    assert [(i.reminder_id, i.reminder_due_at) for i in await queue_items(sessions)] == [(rid, due)]
    row = await store.get(rid)
    assert row is not None and row.status == "fired"


async def test_cancelling_a_series_stops_it(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = Reminders(sessions, LISBON)
    rid = await weekday_briefing(store, lisbon(2026, 11, 16, 8, 0), ["diogo"])
    await store.claim_due(lisbon(2026, 11, 16, 8, 0))
    outcome, _ = await store.cancel(rid, lisbon(2026, 11, 16, 9, 0), "diogo")
    assert outcome == "cancelled"
    assert await store.claim_due(lisbon(2026, 11, 30, 8, 0)) == []
    assert len(await queue_items(sessions)) == 1
