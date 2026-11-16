from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.messages import _STAMP, Inline, strip_stamp, to_messages
from alfred.archive.summaries import recalled
from alfred.archive.tables import ConversationSummary
from alfred.attachments.handles import AttachmentRef, handle
from alfred.conversations.store import History, HistoryEntry
from alfred.conversations.tables import Conversation, Entry, EntryOrigin
from alfred.db.engine import make_engine, make_session_factory
from alfred.kernel.content import ModelImage
from alfred.reminders.store import Reminders
from alfred.turns.queue import busy
from alfred.turns.tables import QueueItem
from tests.fakes import ignore
from tests.helpers import add_queue_item, claim_due, image_file, owner_with_files, pdf_file


def unstamped(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """The messages without their times (stored entries carry the database's clock)."""
    return [{**m, "content": strip_stamp(m["content"])} for m in messages]


async def test_history_survives_restart(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    await History(sessions).append("diogo", "user", "add Dune")
    await History(sessions).append("diogo", "assistant", "Added.")

    # A fresh engine stands in for a restarted process.
    engine = make_engine(database_url)
    try:
        entries = await History(make_session_factory(engine)).recent("diogo", 30)
    finally:
        await engine.dispose()
    assert [e.text for e in entries] == ["add Dune", "Added."]


async def test_bounded_context_window(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    for i in range(50):
        await history.append("diogo", "user", f"m{i}")
    entries = await history.recent("diogo", 30)
    assert [e.text for e in entries] == [f"m{i}" for i in range(20, 50)]


async def test_notices_are_part_of_the_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    await history.append("diogo", "notice", "Nightly backup failed: disk full", source="backup")
    await history.append("diogo", "user", "what's eating the space?")
    messages = unstamped(to_messages(await history.recent("diogo", 30)))
    assert messages == [
        {"role": "user", "content": "[notice:backup] Nightly backup failed: disk full"},
        {"role": "user", "content": "what's eating the space?"},
    ]


async def test_archived_conversations_are_kept(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    await history.append("diogo", "user", "old topic")
    old = await history.current_conversation_id("diogo")
    await history.append("diogo", "assistant", "ok")
    reset = await history.reset("diogo")
    assert reset.archived_id == old
    assert reset.new_id != old

    assert await history.recent("diogo", 30) == []
    async with sessions() as session:
        archived = await session.get(Conversation, old)
        assert archived is not None and archived.ended_at is not None
        assert archived.end_reason == "user"
        assert archived.marker_sent_at is None
        count = await session.scalar(
            select(func.count()).select_from(Entry).where(Entry.conversation_id == old)
        )
    assert count == 2


async def test_notice_after_reset_lands_in_new_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    await history.append("diogo", "user", "before")
    new = (await history.reset("diogo")).new_id
    await history.append("diogo", "notice", "backup done", source="backup")
    assert await history.current_conversation_id("diogo") == new
    assert [e.text for e in await history.recent("diogo", 30)] == ["backup done"]


async def test_reset_without_current_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    reset = await history.reset("diogo")
    assert reset.archived_id is None
    assert await history.current_conversation_id("diogo") == reset.new_id


# --- recalls in the window -------------------------------------------------------


async def add_archived(sessions: async_sessionmaker[AsyncSession], cid: int, title: str) -> None:
    async with sessions.begin() as session:
        session.add(Conversation(id=cid, user_name="diogo", ended_at=func.now()))
        await session.flush()
        session.add(
            ConversationSummary(conversation_id=cid, title=title, summary=f"Summary of {title}.")
        )


async def add_recall(sessions: async_sessionmaker[AsyncSession], cid: int, text_: str) -> None:
    current = await History(sessions).current_conversation_id("diogo")
    async with sessions.begin() as session:
        session.add(
            Entry(
                conversation_id=current,
                kind="recall",
                text=text_,
                recalled_conversation_id=cid,
            )
        )


async def test_recall_in_the_window(sessions: async_sessionmaker[AsyncSession]) -> None:
    await add_archived(sessions, 41, "Chicken thighs recipe")
    history = History(sessions, recalled=recalled)
    await history.append("diogo", "user", "remember the chicken recipe?")
    await add_recall(sessions, 41, '#41 "Chicken thighs recipe", ...\nOwner: chicken?')
    await history.append("diogo", "assistant", "Lemon and garlic, sir.")
    messages = unstamped(to_messages(await history.recent("diogo", 30)))
    assert messages[1] == {
        "role": "user",
        "content": '[recall:#41 "Chicken thighs recipe"] #41 "Chicken thighs recipe", ...\n'
        "Owner: chicken?",
    }


async def test_only_newest_recall_in_full(sessions: async_sessionmaker[AsyncSession]) -> None:
    await add_archived(sessions, 41, "Chicken thighs recipe")
    await add_archived(sessions, 57, "Disk full")
    history = History(sessions, recalled=recalled)
    await add_recall(sessions, 41, "full transcript of 41")
    await history.append("diogo", "user", "and the disk one?")
    await add_recall(sessions, 57, "full transcript of 57")
    messages = [m["content"] for m in unstamped(to_messages(await history.recent("diogo", 30)))]
    assert messages == [
        '[recall:#41 "Chicken thighs recipe"] Summary: Summary of Chicken thighs recipe.',
        "and the disk one?",
        '[recall:#57 "Disk full"] full transcript of 57',
    ]


async def test_recall_counts_as_one_entry(sessions: async_sessionmaker[AsyncSession]) -> None:
    await add_archived(sessions, 41, "Chicken thighs recipe")
    history = History(sessions, recalled=recalled)
    await history.append("diogo", "user", "m1")
    await add_recall(sessions, 41, "line\n" * 1000)
    await history.append("diogo", "user", "m2")
    entries = await history.recent("diogo", 2)
    assert [e.kind for e in entries] == ["recall", "user"]
    assert entries[0].recalled_id == 41
    assert entries[0].recalled_title == "Chicken thighs recipe"
    assert entries[0].recalled_summary == "Summary of Chicken thighs recipe."


# --- reminders -------------------------------------------------------------------

LISBON = ZoneInfo("Europe/Lisbon")
DUE = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)


async def test_reply_to_a_reminder(sessions: async_sessionmaker[AsyncSession]) -> None:
    reminder_id = await Reminders(sessions).set("call mum", DUE, "diogo", ["diogo"])
    history = History(sessions)
    await history.append(
        "diogo", "reminder", "call mum", origin=EntryOrigin(reminder_id, DUE, "diogo")
    )
    await history.append(
        "diogo",
        "reminder_reply",
        "Your call to mum, sir.",
        origin=EntryOrigin(reminder_id, DUE, "diogo"),
    )
    await history.append("diogo", "user", "remind me again in an hour")
    messages = unstamped(to_messages(await history.recent("diogo", 30), LISBON))
    assert messages == [
        {
            "role": "user",
            "content": f"[reminder:#{reminder_id} due Monday 9 November 2026, 17:00] call mum",
        },
        {"role": "assistant", "content": "Your call to mum, sir."},
        {"role": "user", "content": "remind me again in an hour"},
    ]


async def test_reminder_due_in_the_owners_timezone(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    reminder_id = await Reminders(sessions).set("call mum", DUE, "diogo", ["diogo"])
    history = History(sessions)
    await history.append(
        "diogo", "reminder", "call mum", origin=EntryOrigin(reminder_id, DUE, "diogo")
    )
    [message] = unstamped(to_messages(await history.recent("diogo", 30), ZoneInfo("Asia/Tokyo")))
    label = f"[reminder:#{reminder_id} due Tuesday 10 November 2026, 02:00]"
    assert message["content"] == f"{label} call mum"


async def test_reminder_set_in_an_archived_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    await history.append("diogo", "user", "remind me at five to call mum")
    set_in = await history.current_conversation_id("diogo")
    reminder_id = await Reminders(sessions).set("call mum", DUE, "diogo", ["diogo"])
    reset = await history.reset("diogo")
    assert reset.archived_id == set_in

    await history.append(
        "diogo", "reminder", "call mum", origin=EntryOrigin(reminder_id, DUE, "diogo")
    )
    await history.append(
        "diogo",
        "reminder_reply",
        "Your call to mum, sir.",
        origin=EntryOrigin(reminder_id, DUE, "diogo"),
    )
    async with sessions() as session:
        rows = (
            await session.execute(
                select(Entry.conversation_id, Entry.kind).where(
                    Entry.kind.in_(("reminder", "reminder_reply"))
                )
            )
        ).all()
    assert sorted(rows) == [(reset.new_id, "reminder"), (reset.new_id, "reminder_reply")]


async def test_each_delivery_keeps_its_own_due_time(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from alfred.reminders.when import parse_rule

    store = Reminders(sessions, LISBON)
    monday = datetime(2026, 11, 16, 8, 0, tzinfo=LISBON)
    tuesday = datetime(2026, 11, 17, 8, 0, tzinfo=LISBON)
    rule = parse_rule(repeat="weekdays", at="08:00")
    reminder_id = await store.set("brief me", monday, "diogo", ["diogo"], rule=rule)
    history = History(sessions)
    for due in (monday, tuesday):
        await claim_due(store, due)
        await history.append(
            "diogo", "reminder", "brief me", origin=EntryOrigin(reminder_id, due, "diogo")
        )
        await history.append(
            "diogo", "reminder_reply", "Sunny.", origin=EntryOrigin(reminder_id, due, "diogo")
        )
    row = await store.get(reminder_id)
    assert row is not None and row.due_at == datetime(2026, 11, 18, 8, 0, tzinfo=LISBON)
    labels = [
        m["content"]
        for m in unstamped(to_messages(await history.recent("diogo", 30), LISBON))
        if m["role"] == "user"
    ]
    assert labels == [
        f"[reminder:#{reminder_id} due Monday 16 November 2026, 08:00] brief me",
        f"[reminder:#{reminder_id} due Tuesday 17 November 2026, 08:00] brief me",
    ]


async def test_recent_reads_when_each_entry_was_recorded(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    before = datetime.now(UTC)
    await History(sessions).append("diogo", "user", "hi")
    [entry] = await History(sessions).recent("diogo", 30)
    assert entry.created_at is not None
    assert (
        before - timedelta(minutes=1) < entry.created_at < datetime.now(UTC) + timedelta(minutes=1)
    )


async def test_stored_entries_are_stamped(sessions: async_sessionmaker[AsyncSession]) -> None:
    await History(sessions).append("diogo", "user", "hi")
    [message] = to_messages(await History(sessions).recent("diogo", 30), LISBON)
    assert _STAMP.match(message["content"])
    assert strip_stamp(message["content"]) == "hi"


# --- Attachment handles (change add-attachments, design D4) -----------------------

INVOICE_P1 = "Invoice 2026-118 from Lisbon Plumbing, total 240.00 EUR"


async def test_pdf_handle(sessions: async_sessionmaker[AsyncSession]) -> None:
    pdf = pdf_file([INVOICE_P1, *(f"page {n} text" for n in range(2, 13))])
    await owner_with_files(sessions, "file this", [pdf])
    (entry,) = await History(sessions).recent("diogo", 30)
    (ref,) = entry.attachments
    assert (ref.kind, ref.page_count, ref.first_page) == ("pdf", 12, INVOICE_P1)
    assert unstamped(to_messages([entry])) == [
        {
            "role": "user",
            "content": f'file this [attachment #{ref.id}: PDF "invoice.pdf", 12 pages, text]',
        }
    ]


async def test_scanned_pdf_handle_without_caption(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await owner_with_files(sessions, "", [pdf_file(["12"], name=None)])
    (entry,) = await History(sessions).recent("diogo", 30)
    assert unstamped(to_messages([entry])) == [
        {"role": "user", "content": "[attachment #1: PDF, 1 page, scanned]"}
    ]


async def test_owner_entry_with_two_images(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    await history.append("diogo", "user", "earlier")
    await owner_with_files(
        sessions,
        "which is cheapest?",
        [image_file("attachments/a", "shop-a.jpg"), image_file("attachments/b")],
    )
    await history.append("diogo", "assistant", "The second.")
    entries = await history.recent("diogo", 30)
    assert [len(e.attachments) for e in entries] == [0, 2, 0]
    assert [r.object_key for r in entries[1].attachments] == ["attachments/a", "attachments/b"]
    assert unstamped(to_messages(entries)) == [
        {"role": "user", "content": "earlier"},
        {
            "role": "user",
            "content": 'which is cheapest? [attachment #1: image "shop-a.jpg"] '
            "[attachment #2: image]",
        },
        {"role": "assistant", "content": "The second."},
    ]


async def test_inline_entry_gets_image_parts(sessions: async_sessionmaker[AsyncSession]) -> None:
    _, entry_id = await owner_with_files(sessions, "what's this?", [image_file()])
    (entry,) = await History(sessions).recent("diogo", 30)
    image = ModelImage(b"jpeg", "image/jpeg")
    (message,) = to_messages(
        [entry], inline={entry_id: Inline(["[attachment #1: image]"], [image])}
    )
    text_part, image_part = message["content"]
    assert strip_stamp(text_part["text"]) == "what's this? [attachment #1: image]"
    assert _STAMP.match(text_part["text"])
    assert image_part == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,anBlZw=="},
    }


def test_handle_note() -> None:
    ref = AttachmentRef(7, "image", "image/jpeg", "attachments/x", "receipt.jpg")
    assert handle(ref, "this brain can't see images") == (
        '[attachment #7: image "receipt.jpg" (this brain can\'t see images)]'
    )


# --- One current conversation per user (change add-multiple-users) -----------------


async def test_separate_conversations(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    await history.append("diogo", "user", "that chicken recipe?")
    await history.append("diogo", "assistant", "Lemon and garlic.")
    await history.append("ana", "user", "what was that recipe?")
    assert [e.text for e in await history.recent("ana", 30)] == ["what was that recipe?"]
    assert [e.text for e in await history.recent("diogo", 30)] == [
        "that chicken recipe?",
        "Lemon and garlic.",
    ]
    diogo, ana = (
        await history.current_conversation_id("diogo"),
        await history.current_conversation_id("ana"),
    )
    assert diogo != ana
    async with sessions() as session:
        rows = await session.execute(select(Conversation.id, Conversation.user_name))
        owners = {cid: name for cid, name in rows}
    assert owners == {diogo: "diogo", ana: "ana"}


async def test_first_conversation_created_lazily(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    assert await history.recent("ana", 30) == []
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 0
    first = await history.current_conversation_id("ana")
    assert await history.current_conversation_id("ana") == first


async def test_reset_leaves_the_other_user_untouched(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    await history.append("diogo", "user", "still going")
    await history.append("ana", "user", "we're done")
    diogo = await history.current_conversation_id("diogo")
    ana = await history.current_conversation_id("ana")

    reset = await history.reset("ana")
    assert reset.archived_id == ana and reset.new_id not in (ana, diogo)
    assert await history.current_conversation_id("ana") == reset.new_id
    assert await history.current_conversation_id("diogo") == diogo
    assert [e.text for e in await history.recent("diogo", 30)] == ["still going"]
    assert await history.recent("ana", 30) == []


async def test_notice_recorded_for_the_target_user(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    await history.append("diogo", "user", "hi")
    await history.append("ana", "notice", "Dune finished", source="mcparr")
    assert [(e.kind, e.text) for e in await history.recent("ana", 30)] == [
        ("notice", "Dune finished")
    ]
    assert [e.kind for e in await history.recent("diogo", 30)] == ["user"]


async def test_reminder_set_by_someone_else(sessions: async_sessionmaker[AsyncSession]) -> None:
    reminder_id = await Reminders(sessions).set("buy milk", DUE, "diogo", ["ana", "diogo"])
    history = History(sessions)
    for user in ("ana", "diogo"):
        await history.append(
            user, "reminder", "buy milk", origin=EntryOrigin(reminder_id, DUE, "diogo")
        )
    ana = unstamped(to_messages(await history.recent("ana", 30), LISBON))
    diogo = unstamped(to_messages(await history.recent("diogo", 30), LISBON))
    assert ana[0]["content"].startswith(f"[reminder:#{reminder_id} due ")
    assert ana[0]["content"].endswith(", set by diogo] buy milk")
    assert "set by" not in diogo[0]["content"]


# --- idle close (change close-idle-conversations, design D3, D4) -------------------


async def user_wrote_at(
    sessions: async_sessionmaker[AsyncSession], user: str, at: datetime, kind: str = "user"
) -> int:
    """Append an entry for ``user`` and backdate it to ``at``. Returns its conversation."""
    history = History(sessions)
    entry_id = await history.append(user, kind, "hi", source="backup" if kind == "notice" else None)  # type: ignore[arg-type]
    async with sessions.begin() as session:
        await session.execute(update(Entry).where(Entry.id == entry_id).values(created_at=at))
    return await history.current_conversation_id(user)


IDLE_NOW = datetime(2026, 11, 14, 5, 10, tzinfo=UTC)
CUTOFF = IDLE_NOW - timedelta(hours=6)


async def test_close_idle(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    old = await user_wrote_at(sessions, "diogo", CUTOFF - timedelta(minutes=1))
    # A notice after the last user message doesn't keep it open.
    await user_wrote_at(sessions, "diogo", IDLE_NOW - timedelta(hours=3), kind="notice")
    reset = await history.close_idle(old, CUTOFF, busy=busy)
    assert reset is not None and reset.archived_id == old
    assert await history.current_conversation_id("diogo") == reset.new_id
    async with sessions() as session:
        archived = await session.get(Conversation, old)
    assert archived is not None and archived.ended_at is not None
    assert archived.end_reason == "idle" and archived.marker_sent_at is None
    # The notice stays in the archived conversation.
    assert await history.recent("diogo", 30) == []
    assert await history.close_idle(old, CUTOFF, busy=busy) is None  # no longer current


async def test_close_idle_refuses_a_recent_message(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    cid = await user_wrote_at(sessions, "diogo", CUTOFF - timedelta(hours=1))
    await user_wrote_at(sessions, "diogo", CUTOFF + timedelta(minutes=1))
    assert await history.close_idle(cid, CUTOFF, busy=busy) is None
    assert await history.current_conversation_id("diogo") == cid


async def test_close_idle_refuses_only_notices(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    cid = await user_wrote_at(sessions, "diogo", CUTOFF - timedelta(days=3), kind="notice")
    assert await history.last_user_messages() == []
    assert await history.close_idle(cid, CUTOFF, busy=busy) is None
    assert await history.current_conversation_id("diogo") == cid


async def test_close_idle_refuses_while_the_user_has_a_queue_item(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    cid = await user_wrote_at(sessions, "ana", CUTOFF - timedelta(hours=1))
    item = await add_queue_item(sessions, user="ana")
    # Another user's item doesn't matter...
    await add_queue_item(sessions, user="diogo")
    assert await history.close_idle(cid, CUTOFF, busy=busy) is None
    async with sessions.begin() as session:
        await session.execute(update(QueueItem).where(QueueItem.id == item).values(status="done"))
    # ...and once ana's ends, it can close.
    assert await history.close_idle(cid, CUTOFF, busy=busy) is not None


async def test_close_idle_leaves_other_users_alone(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    ana = await user_wrote_at(sessions, "ana", CUTOFF - timedelta(hours=1))
    diogo = await user_wrote_at(sessions, "diogo", CUTOFF - timedelta(hours=1))
    assert [(m.conversation_id, m.user) for m in await history.last_user_messages()] == [
        (ana, "ana"),
        (diogo, "diogo"),
    ]
    assert await history.close_idle(ana, CUTOFF, busy=busy) is not None
    assert await history.current_conversation_id("diogo") == diogo
    assert [e.kind for e in await history.recent("diogo", 30)] == ["user"]


async def test_close_idle_and_a_concurrent_append(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    """An append in flight lands before the close (which is then refused) or after it,
    in the new conversation; never in the archived one."""
    history = History(sessions)
    cid = await user_wrote_at(sessions, "diogo", CUTOFF - timedelta(hours=1))

    # The append holds its FOR SHARE lock: the close waits, then sees its entry.
    other_engine = make_engine(database_url)
    other = make_session_factory(other_engine)
    async with other.begin() as session:
        await session.execute(
            select(Conversation.id).where(Conversation.id == cid).with_for_update(read=True)
        )
        await session.execute(
            insert(Entry).values(conversation_id=cid, kind="user", text="still here")
        )
        closing = asyncio.create_task(history.close_idle(cid, CUTOFF, busy=busy))
        await asyncio.sleep(0.2)
        assert not closing.done()  # waiting for the append's lock
    assert await closing is None
    assert await history.current_conversation_id("diogo") == cid

    # The close holds its lock: the append waits, then lands in the new conversation.
    async with sessions.begin() as session:
        await session.execute(
            update(Entry)
            .where(Entry.conversation_id == cid)
            .values(created_at=CUTOFF - timedelta(hours=1))
        )
    async with other.begin() as session:
        # Stops the close at its queue check, after it has locked the conversation.
        await session.execute(text("LOCK TABLE queue_items IN ACCESS EXCLUSIVE MODE"))
        closing = asyncio.create_task(history.close_idle(cid, CUTOFF, busy=busy))
        await asyncio.sleep(0.2)
        appending = asyncio.create_task(history.append("diogo", "user", "late"))
        await asyncio.sleep(0.2)
        assert not closing.done() and not appending.done()
    reset = await closing
    await appending
    assert reset is not None and reset.archived_id == cid
    assert await history.current_conversation_id("diogo") == reset.new_id
    assert [e.text for e in await history.recent("diogo", 30)] == ["late"]
    await other_engine.dispose()


async def test_take_idle_marker_once(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    cid = await user_wrote_at(sessions, "diogo", CUTOFF - timedelta(hours=1))
    reset = await history.close_idle(cid, CUTOFF, busy=busy)
    assert reset is not None
    assert await history.take_idle_marker("ana") is None
    assert await history.take_idle_marker("diogo") == (cid, reset.new_id)
    assert await history.take_idle_marker("diogo") is None
    async with sessions() as session:
        archived = await session.get(Conversation, cid)
    assert archived is not None and archived.marker_sent_at is not None


async def test_take_idle_marker_claims_older_ones_too(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    first = await user_wrote_at(sessions, "diogo", CUTOFF - timedelta(hours=9))
    assert await history.close_idle(first, CUTOFF, busy=busy) is not None
    second = await user_wrote_at(sessions, "diogo", CUTOFF - timedelta(hours=1))
    reset = await history.close_idle(second, CUTOFF, busy=busy)
    assert reset is not None
    assert await history.take_idle_marker("diogo") == (second, reset.new_id)
    assert await history.take_idle_marker("diogo") is None


async def test_no_idle_marker_after_a_user_reset(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    await history.append("diogo", "user", "we're done")
    await history.reset("diogo")
    assert await history.take_idle_marker("diogo") is None


# --- conversations without user messages (change add-recurring-reminders, D7) -------

SIX = timedelta(hours=6)


async def backdate(
    sessions: async_sessionmaker[AsyncSession], entry_id: int, ago: timedelta
) -> None:
    async with sessions.begin() as session:
        await session.execute(
            update(Entry).where(Entry.id == entry_id).values(created_at=func.now() - ago)
        )


async def conversation_of(sessions: async_sessionmaker[AsyncSession], entry_id: int) -> int:
    async with sessions() as session:
        found = await session.scalar(select(Entry.conversation_id).where(Entry.id == entry_id))
    assert found is not None
    return found


async def end_reason(sessions: async_sessionmaker[AsyncSession], cid: int) -> str | None:
    async with sessions() as session:
        row = await session.get(Conversation, cid)
    assert row is not None
    return row.end_reason


def unattended_history(
    sessions: async_sessionmaker[AsyncSession], archives: list[int] | None = None
) -> History:
    return History(
        sessions,
        idle_after=SIX,
        on_archive=(lambda: archives.append(1)) if archives is not None else ignore,
    )


async def briefing(
    history: History, sessions: async_sessionmaker[AsyncSession], ago: timedelta
) -> int:
    """Yesterday's briefing, as the worker records it, ``ago`` in the past."""
    item = await add_queue_item(sessions, "reminder", "brief me")
    first = await history.append("diogo", "reminder", "brief me", turn_id=item)
    second = await history.append("diogo", "reminder_reply", "Sunny.", turn_id=item)
    await backdate(sessions, first, ago)
    await backdate(sessions, second, ago)
    return await conversation_of(sessions, first)


async def test_unanswered_daily_briefing(sessions: async_sessionmaker[AsyncSession]) -> None:
    archives: list[int] = []
    history = unattended_history(sessions, archives)
    tuesday = await briefing(history, sessions, timedelta(hours=24))

    item = await add_queue_item(sessions, "reminder", "brief me")
    reminder = await history.append("diogo", "reminder", "brief me", turn_id=item)
    reply = await history.append("diogo", "reminder_reply", "Rain.", turn_id=item)
    wednesday = await conversation_of(sessions, reminder)
    assert wednesday != tuesday
    # The reply stays with its reminder, in the new conversation.
    assert await conversation_of(sessions, reply) == wednesday
    assert await history.current_conversation_id("diogo") == wednesday
    assert await end_reason(sessions, tuesday) == "unattended"
    assert archives == [1]
    # No marker, now or later.
    assert await history.take_idle_marker("diogo") is None


async def test_late_reply_keeps_the_briefing(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = unattended_history(sessions)
    cid = await briefing(history, sessions, timedelta(hours=7))
    entry = await history.append("diogo", "user", "more about the second download?")
    assert await conversation_of(sessions, entry) == cid
    assert [e.kind for e in await history.recent("diogo", 30)] == [
        "reminder",
        "reminder_reply",
        "user",
    ]


async def test_burst_of_notices(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = unattended_history(sessions)
    first = await history.append("diogo", "notice", "backup started", source="backup")
    await backdate(sessions, first, timedelta(minutes=10))
    second = await history.append("diogo", "notice", "backup done", source="backup")
    assert await conversation_of(sessions, first) == await conversation_of(sessions, second)


async def test_ask_prompt_closes_an_old_notice_only_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = unattended_history(sessions)
    notice = await history.append("diogo", "notice", "backup done", source="backup")
    await backdate(sessions, notice, timedelta(days=2))
    ask = await history.append("diogo", "ask_prompt", "disk at 93%", source="disk-check")
    assert await conversation_of(sessions, ask) != await conversation_of(sessions, notice)
    assert await end_reason(sessions, await conversation_of(sessions, notice)) == "unattended"


@pytest.mark.parametrize("ago", [timedelta(hours=3), timedelta(days=3)])
async def test_conversation_with_a_user_message_is_not_closed_this_way(
    sessions: async_sessionmaker[AsyncSession], ago: timedelta
) -> None:
    archives: list[int] = []
    history = unattended_history(sessions, archives)
    said = await history.append("diogo", "user", "hello")
    await backdate(sessions, said, ago)
    item = await add_queue_item(sessions, "reminder", "call mum")
    reminder = await history.append("diogo", "reminder", "call mum", turn_id=item)
    assert await conversation_of(sessions, reminder) == await conversation_of(sessions, said)
    assert archives == []


async def test_marker_for_an_earlier_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = unattended_history(sessions)
    said = await history.append("diogo", "user", "goodnight")
    await backdate(sessions, said, timedelta(hours=30))
    c41 = await conversation_of(sessions, said)
    reset = await history.close_idle(c41, datetime.now(UTC) - SIX, busy=busy)
    assert reset is not None
    c42 = await briefing(history, sessions, timedelta(hours=7))
    assert c42 == reset.new_id
    item = await add_queue_item(sessions, "reminder", "brief me")
    c43 = await conversation_of(
        sessions, await history.append("diogo", "reminder", "brief me", turn_id=item)
    )
    assert c43 != c42 and await end_reason(sessions, c42) == "unattended"
    assert await history.take_idle_marker("diogo") == (c41, c43)
    assert await history.take_idle_marker("diogo") is None


async def test_idle_close_off_keeps_the_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    notice = await history.append("diogo", "notice", "backup done", source="backup")
    await backdate(sessions, notice, timedelta(days=30))
    later = await history.append("diogo", "notice", "backup done", source="backup")
    assert await conversation_of(sessions, later) == await conversation_of(sessions, notice)


async def test_retried_reminder_stays_where_it_was_recorded(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    archives: list[int] = []
    history = unattended_history(sessions, archives)
    item = await add_queue_item(sessions, "reminder", "brief me")
    first = await history.append("diogo", "reminder", "brief me", turn_id=item)
    await backdate(sessions, first, timedelta(hours=7))
    # The worker crashed before the reply and retries the item later.
    again = await history.append("diogo", "reminder", "brief me", turn_id=item)
    reply = await history.append("diogo", "reminder_reply", "Sunny.", turn_id=item)
    assert again == first
    assert await conversation_of(sessions, reply) == await conversation_of(sessions, first)
    assert archives == []


# --- the context a parked turn resumes with (change add-user-input-requests) ---------


async def test_recent_up_to_an_entry(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    first = await history.append("ana", "user", "add Dune")
    upto = await history.append("ana", "assistant", "Shall I?")
    await history.append("ana", "notice", "backup done", source="backup")
    conversation_id = await history.current_conversation_id("ana")
    entries = await history.parked_window(
        "ana",
        30,
        conversation_id=conversation_id,
        upto_entry_id=upto,
        entry_ids=(first, upto),
    )
    assert [e.text for e in entries] == ["add Dune", "Shall I?"]
    assert {e.conversation_id for e in entries} == {conversation_id}
    # Without them, the newest entries of the current conversation.
    assert [e.text for e in await history.recent("ana", 30)][-1] == "backup done"


async def test_recent_reads_an_archived_conversation_by_id(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions, idle_after=timedelta(hours=1))
    upto = await history.append("ana", "ask_prompt", "anything to add?", source="jellyfin")
    conversation_id = await history.current_conversation_id("ana")
    # Closed as unattended when a later arrival found it stale.
    async with sessions.begin() as session:
        await session.execute(update(Entry).values(created_at=func.now() - timedelta(hours=2)))
    await history.append("ana", "notice", "later", source="backup")
    assert await history.current_conversation_id("ana") != conversation_id
    window = {
        "conversation_id": conversation_id,
        "upto_entry_id": upto,
        "entry_ids": (upto,),
    }
    entries = await history.parked_window("ana", 30, **window)
    assert [e.text for e in entries] == ["anything to add?"]
    # Never another user's conversation.
    assert await history.parked_window("diogo", 30, **window) == []


async def test_other_parked_turns_are_left_out(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """An entry of a turn waiting for its user's answer has no reply yet (0.16.1)."""
    from alfred.kernel.questions import Option, Question
    from alfred.turns.inputs.store import InputRequests

    history = History(sessions)
    await history.append("ana", "user", "hello")
    await history.append("ana", "assistant", "Good evening.")
    parked = await add_queue_item(sessions, "chat", "add Alien", user="ana")
    await history.append("ana", "user", "add Alien", turn_id=parked)
    requests = InputRequests(sessions)
    request = await requests.park(
        parked, "ana", tool="t", arguments={}, question=Question("?", (Option("Yes", True),)),
        state={}, turn={}, expires_at=timedelta(hours=1),
    )  # fmt: skip
    ask = await add_queue_item(sessions, "ask", "Sonarr needs you", source="mcparr", user="ana")

    def texts(entries: list[HistoryEntry]) -> list[str]:
        return [e.text for e in entries]

    async def seen_by(item: int, limit: int = 30) -> list[HistoryEntry]:
        """What the turn of ``item`` reads, as the worker has it read."""
        hidden = await requests.parked_items("ana") - {item}
        return await history.recent("ana", limit, hidden_items=hidden)

    # Another turn doesn't see it, and it doesn't count toward the window.
    assert texts(await seen_by(ask, 2)) == ["hello", "Good evening."]
    # The parked turn itself does.
    assert texts(await seen_by(parked))[-1] == "add Alien"
    # Answered and running: still going on, still hidden.
    await requests.answer(request, 0, "ana")
    assert "add Alien" not in texts(await seen_by(ask))
    await requests.take_answered(parked, "ana")
    assert "add Alien" not in texts(await seen_by(ask))
    # Once it has ended, everyone sees it with its reply.
    await history.append("ana", "assistant", "Added Alien.", turn_id=parked)
    await requests.finish(request, "ana")
    assert texts(await seen_by(ask))[-2:] == [
        "add Alien",
        "Added Alien.",
    ]


async def test_recent_by_entry_ids(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    first = await history.append("ana", "user", "one")
    await history.append("ana", "user", "two")
    third = await history.append("ana", "user", "three")
    conversation_id = await history.current_conversation_id("ana")
    window = {"conversation_id": conversation_id, "upto_entry_id": third}
    entries = await history.parked_window("ana", 30, entry_ids=[first, third], **window)
    assert [e.text for e in entries] == ["one", "three"]
    assert await history.parked_window("ana", 30, entry_ids=[], **window) == []
