"""Conversation history: each user's current conversation, its entries, and resets.

A user's current conversation is their one row of ``conversations`` with
``ended_at IS NULL``; the first is created when it is first needed. Each
write reads it inside its own transaction, so an entry written around a
reset lands in whichever conversation is current when it commits.

A conversation with no message from its user (only notices, ``/ask`` turns
and reminders) is never closed for inactivity. Instead, when a new notice,
``/ask`` prompt or reminder arrives more than the idle close time after its
newest entry, it is archived as ``unattended`` and the new entry starts a
new conversation, with no marker.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from alfred.attachments.handles import AttachmentRef, refs_by_entry
from alfred.attachments.tables import Attachment
from alfred.conversations.current import current_id
from alfred.conversations.tables import Conversation, Entry
from alfred.reminders.tables import Reminder
from alfred.turns.tables import QueueItem

_EntryKind = Literal[
    "user", "assistant", "notice", "ask_prompt", "ask_reply", "reminder", "reminder_reply"
]
# The first entry of something that arrives without the user: a new one may
# close a conversation that has no user message. Never a reply.
_ARRIVALS = frozenset({"notice", "ask_prompt", "reminder"})
# An input request whose turn is still waiting or going on (a parked turn).


@dataclass(frozen=True)
class HistoryEntry:
    kind: str
    source: str | None
    text: str
    # For recall entries: the recalled conversation, its title and its summary.
    recalled_id: int | None = None
    recalled_title: str | None = None
    recalled_summary: str | None = None
    # For reminder entries: the reminder's number and when it was due.
    reminder_id: int | None = None
    reminder_due_at: datetime | None = None
    # When the entry was recorded; stamped on user messages.
    created_at: datetime | None = None
    entry_id: int | None = None
    # A user entry's files, in message order.
    attachments: tuple[AttachmentRef, ...] = ()
    # For reminder entries: who set it, when not the conversation's user.
    reminder_set_by: str | None = None
    conversation_id: int | None = None


class History:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        idle_after: timedelta | None = None,
        on_archive: Callable[[], None] = lambda: None,
    ) -> None:
        self._sessions = sessions
        # ``[history] idle_close_after_h``; None: unattended conversations stay.
        self._idle_after = idle_after
        # Called after a conversation is archived as unattended (the sweeper nudge);
        # by default nobody is told.
        self._on_archive = on_archive

    async def reaches(self, user: str, entry_id: int) -> bool:
        """Whether the entry is in ``user``'s current conversation or in one recalled into it.

        The attachment tools' conversation scope.
        """
        async with self._sessions.begin() as session:
            current = await current_id(session, user, lock=False)
            recalled = select(Entry.recalled_conversation_id).where(
                Entry.conversation_id == current, Entry.kind == "recall"
            )
            found = await session.scalar(
                select(Entry.id).where(
                    Entry.id == entry_id,
                    or_(
                        Entry.conversation_id == current,
                        Entry.conversation_id.in_(recalled.scalar_subquery()),
                    ),
                )
            )
        return found is not None

    async def current_conversation_id(self, user: str) -> int:
        async with self._sessions.begin() as session:
            return await current_id(session, user, lock=False)

    async def append(
        self,
        user: str,
        kind: _EntryKind,
        text_: str,
        source: str | None = None,
        queue_item_id: int | None = None,
        reminder_id: int | None = None,
        reminder_due_at: datetime | None = None,
    ) -> int:
        """Add an entry to ``user``'s current conversation. Returns the entry's ID.

        With ``queue_item_id``, an entry of the same kind for that item that
        already exists is kept and nothing is added (a retried turn); its ID
        is returned. A user entry takes the attachments queued with its
        item, in the same transaction.

        A notice, ``/ask`` prompt or reminder first archives a current
        conversation with no user message whose newest entry is older than
        the idle close time, and starts a new one.
        """
        archived = False
        async with self._sessions.begin() as session:
            if queue_item_id is not None:
                # Before the close check: a retry never closes the conversation
                # its entry is already in.
                done = await session.scalar(
                    select(Entry.id).where(Entry.queue_item_id == queue_item_id, Entry.kind == kind)
                )
                if done is not None:
                    return done
            if kind in _ARRIVALS and self._idle_after is not None:
                conversation_id = await current_id(session, user, lock=True, exclusive=True)
                if await _unattended(session, conversation_id, self._idle_after):
                    await session.execute(
                        update(Conversation)
                        .where(Conversation.id == conversation_id)
                        .values(ended_at=func.now(), end_reason="unattended")
                    )
                    new = Conversation(user_name=user)
                    session.add(new)
                    await session.flush()
                    conversation_id = new.id
                    archived = True
            else:
                conversation_id = await current_id(session, user, lock=True)
            entry_id = await session.scalar(
                insert(Entry)
                .values(
                    conversation_id=conversation_id,
                    kind=kind,
                    source=source,
                    text=text_,
                    queue_item_id=queue_item_id,
                    reminder_id=reminder_id,
                    reminder_due_at=reminder_due_at,
                )
                .on_conflict_do_nothing(index_elements=["queue_item_id", "kind"])
                .returning(Entry.id)
            )
            if entry_id is None:  # recorded by an earlier attempt that committed meanwhile
                entry_id = await session.scalar(
                    select(Entry.id).where(Entry.queue_item_id == queue_item_id, Entry.kind == kind)
                )
                assert entry_id is not None
            if kind == "user" and queue_item_id is not None:
                await session.execute(
                    update(Attachment)
                    .where(Attachment.queue_item_id == queue_item_id, Attachment.entry_id.is_(None))
                    .values(entry_id=entry_id)
                )
        if archived:
            self._on_archive()
        return entry_id

    async def recent(
        self, user: str, limit: int, *, hidden_items: Collection[int] = ()
    ) -> list[HistoryEntry]:
        """The newest ``limit`` entries of ``user``'s current conversation, oldest first.

        A recall counts as one entry, however long its transcript. The entries
        of the queue items ``hidden_items`` are left out, before the limit.
        """
        where = [Conversation.ended_at.is_(None)]
        if hidden_items:
            where.append(_not_from(hidden_items))
        return await self._read(user, limit, where)

    async def parked_window(
        self,
        user: str,
        limit: int,
        *,
        conversation_id: int | None,
        upto_entry_id: int | None,
        entry_ids: Sequence[int] | None,
        hidden_items: Collection[int] = (),
    ) -> list[HistoryEntry]:
        """The context a turn parked with, read again to resume it.

        ``conversation_id`` is read even if it has been archived since, and
        entries added after ``upto_entry_id`` are left out (``None``: the turn
        had no entries). ``entry_ids`` are exactly the entries it had, whatever
        other parked turns did meanwhile; turns parked by 0.16.0 saved none
        (``None``), and read their window up to ``upto_entry_id``.
        ``hidden_items`` are left out, as in :meth:`recent`.
        """
        where = [
            Conversation.ended_at.is_(None)
            if conversation_id is None
            else Conversation.id == conversation_id,
            Entry.id <= (upto_entry_id or 0),
        ]
        if hidden_items:
            where.append(_not_from(hidden_items))
        if entry_ids is not None:
            where.append(Entry.id.in_(entry_ids))
        return await self._read(user, limit, where)

    async def reset(self, user: str) -> _Reset:
        """Archive ``user``'s current conversation and start a new one for them."""
        async with self._sessions.begin() as session:
            archived_id = await session.scalar(
                update(Conversation)
                .where(Conversation.ended_at.is_(None), Conversation.user_name == user)
                .values(ended_at=func.now(), end_reason="user")
                .returning(Conversation.id)
            )
            new = Conversation(user_name=user)
            session.add(new)
            await session.flush()
            return _Reset(archived_id, new.id)

    async def last_user_messages(self) -> list[_LastMessage]:
        """Every current conversation with a message from its user, and the newest one's time.

        Notices, ``/ask`` turns and reminders don't count.
        """
        async with self._sessions() as session:
            rows = await session.execute(
                select(Conversation.id, Conversation.user_name, func.max(Entry.created_at))
                .join(Entry, Entry.conversation_id == Conversation.id)
                .where(Conversation.ended_at.is_(None), Entry.kind == "user")
                .group_by(Conversation.id, Conversation.user_name)
                .order_by(Conversation.id)
            )
            return [_LastMessage(*row) for row in rows]

    async def close_idle(self, conversation_id: int, cutoff: datetime) -> _Reset | None:
        """Archive a current conversation whose user hasn't written since ``cutoff``.

        Returns ``None`` and changes nothing if it is no longer current, its
        user wrote after ``cutoff`` (or never), or one of its user's
        queue items is queued, running, or waiting for their answer.
        """
        async with self._sessions.begin() as session:
            # Lock first: this waits for an append in flight (it holds FOR
            # SHARE), and the checks below are new statements, so they see
            # what it committed.
            user = await session.scalar(
                select(Conversation.user_name)
                .where(Conversation.id == conversation_id, Conversation.ended_at.is_(None))
                .with_for_update()
            )
            if user is None:
                return None
            newest = await session.scalar(
                select(func.max(Entry.created_at)).where(
                    Entry.conversation_id == conversation_id, Entry.kind == "user"
                )
            )
            if newest is None or newest > cutoff:
                return None
            # A turn waiting for its user's answer is busy too, until it is
            # answered, cancelled or expires.
            busy = await session.scalar(
                select(QueueItem.id)
                .where(QueueItem.status.in_(("pending", "waiting")), QueueItem.user_name == user)
                .limit(1)
            )
            if busy is not None:
                return None
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(ended_at=func.now(), end_reason="idle")
            )
            new = Conversation(user_name=user)
            session.add(new)
            await session.flush()
            return _Reset(conversation_id, new.id)

    async def take_idle_marker(self, user: str) -> tuple[int, int] | None:
        """Claim ``user``'s pending idle-close marker: (archived id, current id), or ``None``.

        Claimed before it is sent, so it is handed out once, even if the
        turn is retried. Older pending markers are claimed with the newest.
        """
        async with self._sessions.begin() as session:
            claimed = await session.scalars(
                update(Conversation)
                .where(
                    Conversation.user_name == user,
                    Conversation.end_reason == "idle",
                    Conversation.marker_sent_at.is_(None),
                )
                .values(marker_sent_at=func.now())
                .returning(Conversation.id)
            )
            ids = list(claimed)
            if not ids:
                return None
            return max(ids), await current_id(session, user, lock=False)

    async def _read(
        self, user: str, limit: int, where: list[ColumnElement[bool]]
    ) -> list[HistoryEntry]:
        """The newest ``limit`` of ``user``'s entries that match ``where``, oldest first."""
        recalled = aliased(Conversation)
        async with self._sessions() as session:
            newest = (
                select(
                    Entry.id,
                    Entry.kind,
                    Entry.source,
                    Entry.text,
                    Entry.recalled_conversation_id,
                    Entry.reminder_id,
                    Entry.reminder_due_at,
                    Entry.created_at,
                    Entry.conversation_id,
                )
                .join(Conversation, Conversation.id == Entry.conversation_id)
                .where(Conversation.user_name == user, *where)
                .order_by(Entry.id.desc())
                .limit(limit)
                .subquery()
            )
            rows = await session.execute(
                select(
                    newest.c.kind,
                    newest.c.source,
                    newest.c.text,
                    newest.c.recalled_conversation_id,
                    recalled.title,
                    recalled.summary,
                    newest.c.reminder_id,
                    # The entry's own occurrence: a repeating reminder's row
                    # has moved on.
                    newest.c.reminder_due_at,
                    newest.c.created_at,
                    newest.c.id,
                    Reminder.created_by,
                    newest.c.conversation_id,
                )
                .outerjoin(recalled, recalled.id == newest.c.recalled_conversation_id)
                .outerjoin(Reminder, Reminder.id == newest.c.reminder_id)
                .order_by(newest.c.id)
            )
            entries = [
                HistoryEntry(
                    *row[:-2],
                    reminder_set_by=row[-2] if row[-2] not in (None, user) else None,
                    conversation_id=row[-1],
                )
                for row in rows
            ]
            user_ids = [e.entry_id for e in entries if e.kind == "user" and e.entry_id]
            refs = await refs_by_entry(session, user_ids)
        return [
            dataclasses.replace(e, attachments=refs[e.entry_id]) if e.entry_id in refs else e
            for e in entries
        ]


@dataclass(frozen=True)
class _Reset:
    """The conversation just archived (``None`` if there was none) and the new one."""

    archived_id: int | None
    new_id: int


@dataclass(frozen=True)
class _LastMessage:
    """A current conversation and when its user last wrote in it."""

    conversation_id: int
    user: str
    at: datetime


def _not_from(items: Collection[int]) -> ColumnElement[bool]:
    """Not an entry of one of the queue items ``items``. Entries of no item pass."""
    return or_(Entry.queue_item_id.is_(None), Entry.queue_item_id.not_in(list(items)))


async def _unattended(session: AsyncSession, conversation_id: int, idle_after: timedelta) -> bool:
    """The conversation has entries, none from its user, the newest older than ``idle_after``."""
    has_user = await session.scalar(
        select(Entry.id)
        .where(Entry.conversation_id == conversation_id, Entry.kind == "user")
        .limit(1)
    )
    if has_user is not None:
        return False
    # NULL (no entries yet) is not stale. The database's clock, like created_at.
    stale = await session.scalar(
        select(func.max(Entry.created_at) < func.now() - idle_after).where(
            Entry.conversation_id == conversation_id
        )
    )
    return bool(stale)
