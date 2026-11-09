from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.engine import make_engine, make_session_factory
from alfred.db.models import Conversation, Entry
from alfred.history import History, HistoryEntry, to_messages
from alfred.reminders.store import Reminders


async def test_history_survives_restart(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    await History(sessions).append("owner", "add Dune")
    await History(sessions).append("assistant", "Added.")

    # A fresh engine stands in for a restarted process.
    engine = make_engine(database_url)
    try:
        entries = await History(make_session_factory(engine)).recent(30)
    finally:
        await engine.dispose()
    assert [e.text for e in entries] == ["add Dune", "Added."]


async def test_bounded_context_window(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    for i in range(50):
        await history.append("owner", f"m{i}")
    entries = await history.recent(30)
    assert [e.text for e in entries] == [f"m{i}" for i in range(20, 50)]


async def test_notices_are_part_of_the_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    await history.append("notice", "Nightly backup failed: disk full", source="backup")
    await history.append("owner", "what's eating the space?")
    messages = to_messages(await history.recent(30))
    assert messages == [
        {"role": "user", "content": "[notice:backup] Nightly backup failed: disk full"},
        {"role": "user", "content": "what's eating the space?"},
    ]


def test_message_mapping() -> None:
    entries = [
        HistoryEntry("owner", None, "hi"),
        HistoryEntry("assistant", None, "hello"),
        HistoryEntry("ask_prompt", "disk-check", "93% full"),
        HistoryEntry("ask_reply", "disk-check", "Disk is filling up"),
    ]
    assert to_messages(entries) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "[ask:disk-check] 93% full"},
        {"role": "assistant", "content": "Disk is filling up"},
    ]


async def test_archived_conversations_are_kept(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    old = await history.append("owner", "old topic")
    await history.append("assistant", "ok")
    reset = await history.reset()
    assert reset.archived_id == old
    assert reset.new_id != old

    assert await history.recent(30) == []
    async with sessions() as session:
        archived = await session.get(Conversation, old)
        assert archived is not None and archived.ended_at is not None
        count = await session.scalar(
            select(func.count()).select_from(Entry).where(Entry.conversation_id == old)
        )
    assert count == 2


async def test_notice_after_reset_lands_in_new_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    await history.append("owner", "before")
    new = (await history.reset()).new_id
    landed = await history.append("notice", "backup done", source="backup")
    assert landed == new
    assert [e.text for e in await history.recent(30)] == ["backup done"]


async def test_reset_without_current_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    reset = await history.reset()
    assert reset.archived_id is None
    assert await history.current_conversation_id() == reset.new_id


# --- recalls in the window -------------------------------------------------------


async def add_archived(sessions: async_sessionmaker[AsyncSession], cid: int, title: str) -> None:
    async with sessions.begin() as session:
        session.add(
            Conversation(id=cid, ended_at=func.now(), title=title, summary=f"Summary of {title}.")
        )


async def add_recall(sessions: async_sessionmaker[AsyncSession], cid: int, text_: str) -> None:
    current = await History(sessions).current_conversation_id()
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
    history = History(sessions)
    await history.append("owner", "remember the chicken recipe?")
    await add_recall(sessions, 41, '#41 "Chicken thighs recipe", ...\nOwner: chicken?')
    await history.append("assistant", "Lemon and garlic, sir.")
    messages = to_messages(await history.recent(30))
    assert messages[1] == {
        "role": "user",
        "content": '[recall:#41 "Chicken thighs recipe"] #41 "Chicken thighs recipe", ...\n'
        "Owner: chicken?",
    }


async def test_only_newest_recall_in_full(sessions: async_sessionmaker[AsyncSession]) -> None:
    await add_archived(sessions, 41, "Chicken thighs recipe")
    await add_archived(sessions, 57, "Disk full")
    history = History(sessions)
    await add_recall(sessions, 41, "full transcript of 41")
    await history.append("owner", "and the disk one?")
    await add_recall(sessions, 57, "full transcript of 57")
    messages = [m["content"] for m in to_messages(await history.recent(30))]
    assert messages == [
        '[recall:#41 "Chicken thighs recipe"] Summary: Summary of Chicken thighs recipe.',
        "and the disk one?",
        '[recall:#57 "Disk full"] full transcript of 57',
    ]


async def test_recall_counts_as_one_entry(sessions: async_sessionmaker[AsyncSession]) -> None:
    await add_archived(sessions, 41, "Chicken thighs recipe")
    history = History(sessions)
    await history.append("owner", "m1")
    await add_recall(sessions, 41, "line\n" * 1000)
    await history.append("owner", "m2")
    entries = await history.recent(2)
    assert [e.kind for e in entries] == ["recall", "owner"]
    assert entries[0].recalled_id == 41
    assert entries[0].recalled_title == "Chicken thighs recipe"
    assert entries[0].recalled_summary == "Summary of Chicken thighs recipe."


def test_recall_without_title() -> None:
    entry = HistoryEntry("recall", None, "transcript", 41)
    assert to_messages([entry]) == [{"role": "user", "content": "[recall:#41] transcript"}]


# --- reminders -------------------------------------------------------------------

LISBON = ZoneInfo("Europe/Lisbon")
DUE = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)


async def test_reply_to_a_reminder(sessions: async_sessionmaker[AsyncSession]) -> None:
    reminder_id = await Reminders(sessions).set("call mum", DUE)
    history = History(sessions)
    await history.append("reminder", "call mum", reminder_id=reminder_id)
    await history.append("reminder_reply", "Your call to mum, sir.", reminder_id=reminder_id)
    await history.append("owner", "remind me again in an hour")
    messages = to_messages(await history.recent(30), LISBON)
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
    reminder_id = await Reminders(sessions).set("call mum", DUE)
    history = History(sessions)
    await history.append("reminder", "call mum", reminder_id=reminder_id)
    [message] = to_messages(await history.recent(30), ZoneInfo("Asia/Tokyo"))
    label = f"[reminder:#{reminder_id} due Tuesday 10 November 2026, 02:00]"
    assert message["content"] == f"{label} call mum"


async def test_reminder_set_in_an_archived_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    await history.append("owner", "remind me at five to call mum")
    set_in = await history.current_conversation_id()
    reminder_id = await Reminders(sessions).set("call mum", DUE)
    reset = await history.reset()
    assert reset.archived_id == set_in

    delivered_in = await history.append("reminder", "call mum", reminder_id=reminder_id)
    await history.append("reminder_reply", "Your call to mum, sir.", reminder_id=reminder_id)
    assert delivered_in == reset.new_id
    async with sessions() as session:
        rows = (
            await session.execute(
                select(Entry.conversation_id, Entry.kind).where(Entry.reminder_id == reminder_id)
            )
        ).all()
    assert sorted(rows) == [(reset.new_id, "reminder"), (reset.new_id, "reminder_reply")]
