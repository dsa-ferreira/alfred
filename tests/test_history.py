from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.engine import make_engine, make_session_factory
from alfred.db.models import Conversation, Entry
from alfred.history import History, HistoryEntry, to_messages


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
    new = await history.reset()
    assert new != old

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
    new = await history.reset()
    landed = await history.append("notice", "backup done", source="backup")
    assert landed == new
    assert [e.text for e in await history.recent(30)] == ["backup done"]


async def test_reset_without_current_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = History(sessions)
    new = await history.reset()
    assert await history.current_conversation_id() == new
