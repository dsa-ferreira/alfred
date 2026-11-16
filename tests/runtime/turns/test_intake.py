"""Queuing a chat message with its files in one transaction (design D5)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.features.attachments import NewAttachment
from alfred.features.attachments.tables import Attachment
from alfred.runtime.turns.intake import ChatIntake
from alfred.runtime.turns.queue import Queue
from alfred.runtime.turns.tables import QueueItem


def photo(key: str) -> NewAttachment:
    return NewAttachment("image", "image/jpeg", 1000, key, width=10, height=10)


async def test_items_carry_their_user(sessions: async_sessionmaker[AsyncSession]) -> None:
    queue = Queue(sessions)
    await ChatIntake(sessions, queue).enqueue_user(
        "ana", "what's this?", [photo("attachments/a"), photo("attachments/b")]
    )
    await queue.enqueue_ask("mcparr", "Dune finished", "ana")
    async with sessions() as session:
        items = list(await session.scalars(select(QueueItem).order_by(QueueItem.id)))
        attachments = list(await session.scalars(select(Attachment).order_by(Attachment.id)))
    assert [(i.kind, i.user_name, i.payload) for i in items] == [
        ("chat", "ana", {"text": "what's this?", "attachment_ids": [a.id for a in attachments]}),
        ("ask", "ana", {"source": "mcparr", "prompt": "Dune finished"}),
    ]
    assert [(a.user_name, a.object_key) for a in attachments] == [
        ("ana", "attachments/a"),
        ("ana", "attachments/b"),
    ]


async def test_cancels_waiting_turns_first(sessions: async_sessionmaker[AsyncSession]) -> None:
    called: list[str] = []

    async def before(user: str) -> None:
        async with sessions() as session:
            assert await session.scalar(select(QueueItem.id)) is None
        called.append(user)

    await ChatIntake(sessions, Queue(sessions), before_message=before).enqueue_user("ana", "hi")
    assert called == ["ana"]


async def test_failing_insert_leaves_neither_item_nor_files(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    queue = Queue(sessions)
    chat = ChatIntake(sessions, queue)
    # Two files with one object key: the second insert breaks the unique key.
    with pytest.raises(IntegrityError):
        await chat.enqueue_user("ana", "both", [photo("attachments/x"), photo("attachments/x")])
    async with sessions() as session:
        assert list(await session.scalars(select(QueueItem))) == []
        assert list(await session.scalars(select(Attachment))) == []
    assert not queue.activity.arrived.is_set()
