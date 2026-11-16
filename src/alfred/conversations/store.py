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
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import ColumnElement, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.attachments.handles import AttachmentRef
from alfred.conversations.current import current_id
from alfred.conversations.entry_files import link, refs_by_entry
from alfred.conversations.tables import Conversation, Entry, EntryAttachment, EntryOrigin
from alfred.conversations.transcript import Recalled, RecalledLookup, recalled_of

EntryKind = Literal[
    "user", "assistant", "notice", "ask_prompt", "ask_reply", "reminder", "reminder_reply"
]
# The first entry of something that arrives without the user: a new one may
# close a conversation that has no user message. Never a reply.
_ARRIVALS = frozenset({"notice", "ask_prompt", "reminder"})
# Whether a user has a queue item queued, running or waiting for their answer, read
# in the caller's transaction (``turns.queue.busy``).
Busy = Callable[[AsyncSession, str], Awaitable[bool]]


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
        recalled: RecalledLookup | None = None,
    ) -> None:
        self._sessions = sessions
        # Recalled conversations' titles and summaries (``archive``); without
        # it, recall entries have neither.
        self._recalled = recalled
        # ``[history] idle_close_after_h``; None: unattended conversations stay.
        self._idle_after = idle_after
        # Called after a conversation is archived as unattended (the sweeper nudge);
        # by default nobody is told.
        self._on_archive = on_archive

    async def reaches(self, user: str, attachment_id: int) -> bool:
        """Whether the attachment was sent with an entry of ``user``'s current
        conversation or of one recalled into it.

        The attachment tools' conversation scope.
        """
        async with self._sessions.begin() as session:
            current = await current_id(session, user, lock=False)
            recalled = select(Entry.recalled_conversation_id).where(
                Entry.conversation_id == current, Entry.kind == "recall"
            )
            found = await session.scalar(
                select(Entry.id)
                .join(EntryAttachment, EntryAttachment.entry_id == Entry.id)
                .where(
                    EntryAttachment.attachment_id == attachment_id,
                    or_(
                        Entry.conversation_id == current,
                        Entry.conversation_id.in_(recalled.scalar_subquery()),
                    ),
                )
                .limit(1)
            )
        return found is not None

    async def current_conversation_id(self, user: str) -> int:
        async with self._sessions.begin() as session:
            return await current_id(session, user, lock=False)

    async def append(
        self,
        user: str,
        kind: EntryKind,
        text_: str,
        source: str | None = None,
        turn_id: int | None = None,
        origin: EntryOrigin | None = None,
        attachment_ids: Sequence[int] = (),
    ) -> int:
        """Add an entry to ``user``'s current conversation. Returns the entry's ID.

        With ``turn_id``, an entry of the same kind for that turn that
        already exists is kept and nothing is added (a retried turn); its ID
        is returned. ``attachment_ids`` are a user entry's files, in message
        order, linked in the same transaction.

        A notice, ``/ask`` prompt or reminder first archives a current
        conversation with no user message whose newest entry is older than
        the idle close time, and starts a new one.
        """
        archived = False
        async with self._sessions.begin() as session:
            if turn_id is not None:
                # Before the close check: a retry never closes the conversation
                # its entry is already in.
                done = await session.scalar(
                    select(Entry.id).where(Entry.turn_id == turn_id, Entry.kind == kind)
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
                    turn_id=turn_id,
                    origin=origin.to_json() if origin is not None else None,
                )
                .on_conflict_do_nothing(index_elements=["turn_id", "kind"])
                .returning(Entry.id)
            )
            if entry_id is None:  # recorded by an earlier attempt that committed meanwhile
                entry_id = await session.scalar(
                    select(Entry.id).where(Entry.turn_id == turn_id, Entry.kind == kind)
                )
                assert entry_id is not None
            else:
                await link(session, entry_id, attachment_ids)
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
        entry_ids: Sequence[int],
        hidden_items: Collection[int] = (),
    ) -> list[HistoryEntry]:
        """The context a turn parked with, read again to resume it.

        ``conversation_id`` is read even if it has been archived since, and
        entries added after ``upto_entry_id`` are left out (``None``: the turn
        had no entries). ``entry_ids`` are exactly the entries it had, whatever
        other parked turns did meanwhile. ``hidden_items`` are left out, as in
        :meth:`recent`.
        """
        where = [
            Conversation.ended_at.is_(None)
            if conversation_id is None
            else Conversation.id == conversation_id,
            Entry.id <= (upto_entry_id or 0),
            Entry.id.in_(entry_ids),
        ]
        if hidden_items:
            where.append(_not_from(hidden_items))
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

    async def close_idle(
        self, conversation_id: int, cutoff: datetime, *, busy: Busy
    ) -> _Reset | None:
        """Archive a current conversation whose user hasn't written since ``cutoff``.

        Returns ``None`` and changes nothing if it is no longer current, its
        user wrote after ``cutoff`` (or never), or ``busy`` says one of its
        user's queue items is queued, running, or waiting for their answer.
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
            if await busy(session, user):
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
        async with self._sessions() as session:
            newest = (
                select(
                    Entry.id,
                    Entry.kind,
                    Entry.source,
                    Entry.text,
                    Entry.recalled_conversation_id,
                    Entry.origin,
                    Entry.created_at,
                    Entry.conversation_id,
                )
                .join(Conversation, Conversation.id == Entry.conversation_id)
                .where(Conversation.user_name == user, *where)
                .order_by(Entry.id.desc())
                .limit(limit)
                .subquery()
            )
            rows = list(await session.execute(select(newest).order_by(newest.c.id)))
            recalls = await recalled_of(
                session, self._recalled, (row.recalled_conversation_id for row in rows)
            )
            entries = [_history_entry(user, row, recalls) for row in rows]
            user_ids = [e.entry_id for e in entries if e.kind == "user" and e.entry_id]
            refs = await refs_by_entry(session, user_ids)
        return [
            dataclasses.replace(e, attachments=refs[e.entry_id]) if e.entry_id in refs else e
            for e in entries
        ]


async def append_recall(
    session: AsyncSession, conversation_id: int, recalled_id: int, text_: str
) -> bool:
    """Recall ``recalled_id`` into ``conversation_id``, in the caller's transaction.

    Returns ``False`` and adds nothing if that conversation is already
    recalled into it.
    """
    inserted = await session.execute(
        insert(Entry)
        .values(
            conversation_id=conversation_id,
            kind="recall",
            text=text_,
            recalled_conversation_id=recalled_id,
        )
        .on_conflict_do_nothing(
            index_elements=["conversation_id", "recalled_conversation_id"],
            index_where=text("kind = 'recall'"),
        )
    )
    return bool(inserted.rowcount)  # type: ignore[attr-defined]


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


def _history_entry(user: str, row: Any, recalls: dict[int, Recalled]) -> HistoryEntry:
    """A row of the window query as a :class:`HistoryEntry`."""
    recalled = recalls.get(row.recalled_conversation_id, Recalled())
    origin = EntryOrigin.from_json(row.origin) if row.origin is not None else None
    return HistoryEntry(
        row.kind,
        row.source,
        row.text,
        recalled_id=row.recalled_conversation_id,
        recalled_title=recalled.title,
        recalled_summary=recalled.summary,
        reminder_id=origin.reminder_id if origin else None,
        # The entry's own occurrence: a repeating reminder has moved on.
        reminder_due_at=origin.due_at if origin else None,
        created_at=row.created_at,
        entry_id=row.id,
        reminder_set_by=origin.created_by if origin and origin.created_by != user else None,
        conversation_id=row.conversation_id,
    )


def _not_from(items: Collection[int]) -> ColumnElement[bool]:
    """Not an entry of one of the turns ``items``. Entries of no turn pass."""
    return or_(Entry.turn_id.is_(None), Entry.turn_id.not_in(list(items)))


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
