"""The conversations tables.

- ``conversations``: at most one per user has ``ended_at IS NULL`` (their
  current one). ``end_reason`` says how an archived one ended (``user``,
  ``idle``, or ``unattended`` for one with no user message closed when
  something new arrived), and ``marker_sent_at`` when its idle-close
  marker was handed out.
- ``entries``: what was said in a conversation, in order. Entries written by
  a turn carry its ``turn_id`` (its queue item), so a retried turn records
  them once. A ``recall`` entry brings an archived conversation into the
  current one. ``reminder`` and ``reminder_reply`` entries record the
  occurrence they delivered in ``origin`` (:class:`EntryOrigin`).
- ``entry_attachments``: a user entry's files, in message order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alfred.attachments.tables import Attachment
from alfred.db.base import Base, in_values

# ``user``: written by the conversation's user.
_ENTRY_KINDS = (
    "user",
    "assistant",
    "notice",
    "ask_prompt",
    "ask_reply",
    "recall",
    "reminder",
    "reminder_reply",
)
_ENTRY_TSV = "to_tsvector('english', text)"
_END_REASONS = ("user", "idle", "unattended")


@dataclass(frozen=True)
class EntryOrigin:
    """The reminder occurrence a ``reminder`` or ``reminder_reply`` entry delivered.

    What arrived, as it was then: no link to the ``reminders`` row.
    """

    reminder_id: int
    due_at: datetime
    # Who set the reminder.
    created_by: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> EntryOrigin:
        return cls(data["reminder_id"], datetime.fromisoformat(data["due_at"]), data["created_by"])

    def to_json(self) -> dict[str, Any]:
        return {
            "reminder_id": self.reminder_id,
            "due_at": self.due_at.isoformat(),
            "created_by": self.created_by,
        }


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        # Only one row per user may be current.
        Index(
            "uq_conversations_one_current_per_user",
            "user_name",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        CheckConstraint(in_values("end_reason", _END_REASONS), name="end_reason"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_name: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # How it ended (null while current).
    end_reason: Mapped[str | None] = mapped_column(Text)
    # When the idle-close marker was handed to the user's next message.
    marker_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Entry(Base):
    __tablename__ = "entries"
    __table_args__ = (
        CheckConstraint(in_values("kind", _ENTRY_KINDS), name="kind"),
        Index("ix_entries_conversation_id_id", "conversation_id", "id"),
        # A retried turn must not record its user message (or reply) twice.
        Index("uq_entries_turn_id_kind", "turn_id", "kind", unique=True),
        Index("ix_entries_text_tsv", text(_ENTRY_TSV), postgresql_using="gin"),
        # A conversation is recalled at most once into the same conversation.
        Index(
            "uq_entries_recall",
            "conversation_id",
            "recalled_conversation_id",
            unique=True,
            postgresql_where=text("kind = 'recall'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # The turn (queue item) that wrote this entry; null for notices. Not a
    # foreign key: queue items belong to ``turns``, above this slice.
    turn_id: Mapped[int | None] = mapped_column(BigInteger)
    # For ``recall`` entries: the archived conversation brought back.
    recalled_conversation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("conversations.id")
    )
    # For ``reminder`` and ``reminder_reply`` entries: an :class:`EntryOrigin`.
    origin: Mapped[Any] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EntryAttachment(Base):
    """A user entry's file."""

    __tablename__ = "entry_attachments"
    __table_args__ = (
        # The attachment tools ask which entry a file belongs to.
        Index("ix_entry_attachments_attachment_id", "attachment_id"),
    )

    entry_id: Mapped[int] = mapped_column(BigInteger, ForeignKey(Entry.id), primary_key=True)
    attachment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(Attachment.id), primary_key=True
    )
    # Order within the message (an album).
    position: Mapped[int] = mapped_column(Integer, nullable=False)
