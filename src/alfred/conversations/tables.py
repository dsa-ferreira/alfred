"""The conversations tables.

- ``conversations``: at most one per user has ``ended_at IS NULL`` (their
  current one). ``end_reason`` says how an archived one ended (``user``,
  ``idle``, or ``unattended`` for one with no user message closed when
  something new arrived), and ``marker_sent_at`` when its idle-close
  marker was handed out. Archived ones get a title, a summary and an
  embedding of both; ``search_tsv`` is generated from title and summary.
  A conversation's ``facts_outcome`` is ``NULL`` while its fact extraction
  is pending.
- ``entries``: what was said in a conversation, in order. Entries written by
  a turn carry its ``queue_item_id``, so a retried turn records them once.
  A ``recall`` entry brings an archived conversation into the current one.
  ``reminder`` and ``reminder_reply`` entries point at their reminder
  through ``reminder_id`` and carry the delivered occurrence's
  ``reminder_due_at``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

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
# What the summary's full-text column is generated from.
_SEARCH_TSV = "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(summary, ''))"
_ENTRY_TSV = "to_tsvector('english', text)"
_END_REASONS = ("user", "idle", "unattended")
_FACTS_OUTCOMES = ("done", "gave_up")


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
        Index("ix_conversations_search_tsv", "search_tsv", postgresql_using="gin"),
        CheckConstraint(in_values("end_reason", _END_REASONS), name="end_reason"),
        CheckConstraint(in_values("facts_outcome", _FACTS_OUTCOMES), name="facts_outcome"),
        # Archived conversations whose fact extraction is pending.
        Index(
            "ix_conversations_facts_pending",
            "user_name",
            "id",
            postgresql_where=text("ended_at IS NOT NULL AND facts_outcome IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_name: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    summarised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # No fixed dimension: each row records the model that made it.
    embedding: Mapped[Any] = mapped_column(Vector(), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text)
    # Background retries of the summary or embedding (the sweeper).
    summary_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    summary_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed(_SEARCH_TSV, persisted=True))
    # How it ended (null while current, and for rows archived before 0006).
    end_reason: Mapped[str | None] = mapped_column(Text)
    # When the idle-close marker was handed to the user's next message.
    marker_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Fact extraction: null while pending.
    facts_outcome: Mapped[str | None] = mapped_column(Text)
    facts_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    facts_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Entry(Base):
    __tablename__ = "entries"
    __table_args__ = (
        CheckConstraint(in_values("kind", _ENTRY_KINDS), name="kind"),
        Index("ix_entries_conversation_id_id", "conversation_id", "id"),
        # A retried turn must not record its user message (or reply) twice.
        Index("uq_entries_queue_item_id_kind", "queue_item_id", "kind", unique=True),
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
    # The queue item whose turn wrote this entry (null for notices).
    queue_item_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("queue_items.id"))
    # For ``recall`` entries: the archived conversation brought back.
    recalled_conversation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("conversations.id")
    )
    # For ``reminder`` and ``reminder_reply`` entries: the reminder delivered.
    reminder_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("reminders.id"))
    # For ``reminder`` and ``reminder_reply`` entries: the occurrence's due time.
    reminder_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
