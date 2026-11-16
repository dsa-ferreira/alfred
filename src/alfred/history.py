"""Conversation history: each user's current conversation, its entries, and resets.

A user's current conversation is their one row of ``conversations`` with
``ended_at IS NULL`` (change add-multiple-users, design D5); the first is
created when it is first needed. Each write reads it inside its own
transaction, so an entry written around a reset lands in whichever
conversation is current when it commits.

A conversation with no message from its user (only notices, ``/ask`` turns
and reminders) is never closed for inactivity. Instead, when a new notice,
``/ask`` prompt or reminder arrives more than the idle close time after its
newest entry, it is archived as ``unattended`` and the new entry starts a new
conversation, with no marker (change add-recurring-reminders, design D7).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, select, text, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from alfred.attachments.handles import AttachmentRef, handle, refs_by_entry, with_handles
from alfred.db.models import (
    Attachment,
    Conversation,
    Entry,
    InputRequest,
    QueueItem,
    Reminder,
)
from alfred.memory.transcript import recall_label
from alfred.reminders.when import format_when
from alfred.tools.base import ModelImage

_EntryKind = Literal[
    "user", "assistant", "notice", "ask_prompt", "ask_reply", "reminder", "reminder_reply"
]
_UTC_ZONE = ZoneInfo("UTC")
# The first entry of something that arrives without the user: a new one may
# close a conversation that has no user message (design D7). Never a reply.
_ARRIVALS = frozenset({"notice", "ask_prompt", "reminder"})
# An input request whose turn is still waiting or going on (a parked turn).
_LIVE_STATUSES = ("open", "answered", "running")


# --- message times (change add-utility-tools, design D1) ---------------------

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_STAMP = re.compile(
    r"\[(?:" + "|".join(_DAYS) + r") \d{1,2} (?:" + "|".join(_MONTHS) + r") \d{2}:\d{2}\]"
)
_LEADING_STAMP = re.compile(_STAMP.pattern + r"\s*")


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
    # When the entry was recorded; stamped on user messages (change add-utility-tools, D1).
    created_at: datetime | None = None
    entry_id: int | None = None
    # A user entry's files, in message order (change add-attachments, design D4).
    attachments: tuple[AttachmentRef, ...] = ()
    # For reminder entries: who set it, when not the conversation's user.
    reminder_set_by: str | None = None
    conversation_id: int | None = None


@dataclass(frozen=True)
class Inline:
    """How a user entry in the inline window is sent (change add-attachments, design D4).

    ``handles`` replace the plain handles (a PDF's with its page 1 text, a
    note when an image can't be shown); ``images`` follow the text as image
    parts.
    """

    handles: list[str]
    images: list[ModelImage] = field(default_factory=list)


class History:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        idle_after: timedelta | None = None,
        on_archive: Callable[[], None] | None = None,
    ) -> None:
        self._sessions = sessions
        # ``[history] idle_close_after_h``; None: unattended conversations stay.
        self._idle_after = idle_after
        # Called after a conversation is archived as unattended (the sweeper nudge).
        self._on_archive = on_archive

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
        item, in the same transaction (change add-attachments, design D2).

        A notice, ``/ask`` prompt or reminder first archives a current
        conversation with no user message whose newest entry is older than
        the idle close time, and starts a new one (design D7).
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
        if archived and self._on_archive is not None:
            self._on_archive()
        return entry_id

    async def recent(
        self,
        user: str,
        limit: int,
        *,
        conversation_id: int | None = None,
        upto_entry_id: int | None = None,
        turn_item_id: int | None = None,
        entry_ids: Sequence[int] | None = None,
    ) -> list[HistoryEntry]:
        """The newest ``limit`` entries of ``user``'s current conversation, oldest first.

        A recall counts as one entry, however long its transcript.

        A resumed turn reads the context it parked with (change
        add-user-input-requests, design D3): ``conversation_id`` reads that
        conversation of ``user``'s even if it has been archived since, and
        ``upto_entry_id`` leaves out entries added after that one.

        With ``turn_item_id`` (the queue item of the turn reading it), the
        entries of other turns still waiting for their user's answer are
        left out, before the limit: they have no reply yet, and this turn
        must not act on them. ``entry_ids`` reads exactly those entries
        (of ``conversation_id``): the context a turn parked with.
        """
        if conversation_id is None:
            which = Conversation.ended_at.is_(None)
        else:
            which = Conversation.id == conversation_id
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
                .where(which, Conversation.user_name == user)
                .where(Entry.id <= upto_entry_id if upto_entry_id is not None else true())
                .where(_not_parked(turn_item_id) if turn_item_id is not None else true())
                .where(Entry.id.in_(entry_ids) if entry_ids is not None else true())
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
                    # has moved on (change add-recurring-reminders, D7).
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

    # --- idle close (change close-idle-conversations, design D3, D4) ----------

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
            # answered, cancelled or expires (change add-user-input-requests).
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


async def current_id(
    session: AsyncSession, user: str, *, lock: bool, exclusive: bool = False
) -> int:
    """``user``'s current conversation, created if they have none yet.

    ``exclusive`` takes it ``FOR UPDATE`` instead of ``FOR SHARE``, to archive
    it in the same transaction (design D7); two arrivals for the same user
    then take turns, so only one of them closes it.
    """
    query = select(Conversation.id).where(
        Conversation.ended_at.is_(None), Conversation.user_name == user
    )
    if lock:
        # A reset's UPDATE waits for this transaction, so the entry can't land
        # in a conversation that was archived before it committed.
        query = query.with_for_update(read=not exclusive)
    found = await session.scalar(query)
    if found is not None:
        return found
    await session.execute(
        insert(Conversation)
        .values(user_name=user)
        .on_conflict_do_nothing(index_elements=["user_name"], index_where=text("ended_at IS NULL"))
    )
    found = await session.scalar(query)
    assert found is not None
    return found


def stamped(text_: str, moment: datetime | None, zone: ZoneInfo) -> str:
    """``text_`` after the time it arrived, or as is when that time is unknown."""
    return text_ if moment is None else f"{_format_stamp(moment, zone)} {text_}"


def strip_stamp(text_: str) -> str:
    """``text_`` without a stamp at its very start (one the model copied, design D2)."""
    return _LEADING_STAMP.sub("", text_, count=1) if _LEADING_STAMP.match(text_) else text_


def to_messages(
    entries: list[HistoryEntry],
    zone: ZoneInfo = _UTC_ZONE,
    inline: Mapping[int, Inline] | None = None,
) -> list[dict[str, Any]]:
    """Map entries to chat-completions messages.

    User messages and ask prompts are ``user`` turns (ask prompts prefixed
    ``[ask:<service>]``), replies are ``assistant`` turns, and notices are
    ``user`` turns prefixed ``[notice:<service>]``. Delivered reminders are
    ``user`` turns prefixed ``[reminder:#<id> due <time in zone>]`` (with
    ``, set by <name>`` when someone else set it), followed by what the user
    was told. Recalls are ``user`` turns
    prefixed ``[recall:#<id> "<title>"]``: the newest with its transcript,
    older ones with only the recalled conversation's summary (memory design D9).

    Every ``user`` turn starts with the time its entry was recorded, before
    its label (``[Mon 9 Nov 14:05] [notice:backup] ...``); replies have none.

    A user message ends with the handles of its attachments
    (``[attachment #7: image "receipt.jpg"]``). The user entries in
    ``inline`` (by entry ID) are sent as it says; with images, their content
    is a list: the text part, then one ``image_url`` part per image.
    """
    newest_recall = max((i for i, e in enumerate(entries) if e.kind == "recall"), default=None)
    messages: list[dict[str, Any]] = []
    for i, e in enumerate(entries):
        content: str | None = None
        match e.kind:
            case "recall":
                label = f"[recall:{recall_label(e.recalled_id, e.recalled_title)}]"
                if i == newest_recall:
                    content = f"{label} {e.text}"
                else:
                    content = f"{label} Summary: {e.recalled_summary or '(no summary yet)'}"
            case "user":
                extra = (inline or {}).get(e.entry_id) if e.entry_id is not None else None
                content = _user_text(e, extra)
                if extra is not None and extra.images:
                    text_part = {"type": "text", "text": stamped(content, e.created_at, zone)}
                    parts = [text_part, *(image.part() for image in extra.images)]
                    messages.append({"role": "user", "content": parts})
                    continue
            case "ask_prompt":
                content = f"[ask:{e.source}] {e.text}"
            case "notice":
                content = f"[notice:{e.source}] {e.text}"
            case "reminder":
                due = f" due {format_when(e.reminder_due_at, zone)}" if e.reminder_due_at else ""
                set_by = f", set by {e.reminder_set_by}" if e.reminder_set_by else ""
                content = f"[reminder:#{e.reminder_id}{due}{set_by}] {e.text}"
            case "assistant" | "ask_reply" | "reminder_reply":
                messages.append({"role": "assistant", "content": e.text})
        if content is not None:
            messages.append({"role": "user", "content": stamped(content, e.created_at, zone)})
    return messages


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


def _not_parked(turn_item_id: int) -> ColumnElement[bool]:
    """Not an entry of another queue item with a live input request (a parked turn)."""
    live = (
        select(InputRequest.id)
        .where(
            InputRequest.queue_item_id == Entry.queue_item_id,
            InputRequest.queue_item_id != turn_item_id,
            InputRequest.status.in_(_LIVE_STATUSES),
        )
        .exists()
    )
    return ~live


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


def _format_stamp(moment: datetime, zone: ZoneInfo) -> str:
    """``[Mon 9 Nov 14:05]``: absolute, so an entry's text never changes.

    English names without the locale, like the rest of alfred's prompts.
    """
    local = moment.astimezone(zone)
    return f"[{_DAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]} {local:%H:%M}]"


def _user_text(entry: HistoryEntry, inline: Inline | None = None) -> str:
    """What the user wrote, then the handles of what they sent with it."""
    handles = inline.handles if inline else [handle(a) for a in entry.attachments]
    return with_handles(entry.text, handles)
