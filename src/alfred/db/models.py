"""SQLAlchemy models (design D7).

Rows that belong to a user name them in ``user_name`` (``created_by`` for
reminders): a configured ``[[users]]`` name (change add-multiple-users,
design D1). There is no users table; entries belong to a user through their
conversation.

- ``conversations``: at most one per user has ``ended_at IS NULL`` (their
  current one).
  ``end_reason`` says how an archived one ended (``user``, ``idle``, or
  ``unattended`` for one with no user message closed when something new
  arrived), and ``marker_sent_at`` when its idle-close marker was handed out
  (change close-idle-conversations, design D2).
  Archived ones get a title, a summary and an embedding of both (memory
  design D1); ``search_tsv`` is generated from title and summary.
- ``entries``: what was said in a conversation, in order. Entries written by
  a turn carry its ``queue_item_id``, so a retried turn records them once.
  A ``recall`` entry brings an archived conversation into the current one
  (memory design D8).
- ``queue_items``: AI-bound work (users' messages, ``/ask`` prompts and due
  reminders), each for one user.
- ``reminders``: timed reminders a user set (change add-reminders, design
  D3), for the users in ``reminder_recipients``. ``fired`` means handed to
  the queue, one item per recipient. A repeating one (``repeat`` set; change
  add-recurring-reminders, design D1) stays ``pending`` and its ``due_at``
  moves to the next occurrence. A reminder queue item and its
  ``reminder``/``reminder_reply`` entries point at the row through
  ``reminder_id`` and carry the delivered occurrence's ``reminder_due_at``.
- ``turn_tool_calls``: the tool calls each turn made, for auditing.
- ``input_requests``: a parked turn's question to its user and the turn's
  saved progress (change add-user-input-requests, design D4). Its queue item
  is ``waiting`` while the request is ``open``; at most one request per item
  is live (``open``, ``answered`` or ``running``).
- ``attachments``: users' photos, image files and PDFs (change
  add-attachments, design D1). The bytes are in the object store under
  ``object_key``; the row is written with its queue item and linked to the
  user entry (``entry_id``) when the turn records it. ``attachment_pages``
  holds each PDF page's text.
- ``facts``: short facts about a user (change add-user-facts, design D1),
  ``told`` by the user or ``learned`` from an archived conversation. Removal
  is soft: ``removed_at``, ``removed_reason`` and, for replaced and duplicate
  facts, ``replaced_by``. A conversation's ``facts_outcome`` is ``NULL``
  while its fact extraction is pending.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names, so migrations can refer to them.
_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# ``user``: written by the conversation's user (change add-multiple-users, D1).
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
# What the summary's full-text column is generated from; also used by search.
_SEARCH_TSV = "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(summary, ''))"
_ENTRY_TSV = "to_tsvector('english', text)"
_QUEUE_KINDS = ("user", "ask", "reminder")
_REMINDER_STATUSES = ("pending", "fired", "cancelled")
# ``waiting``: parked for its user's answer; ``cancelled``: ended without one
# (change add-user-input-requests, design D4).
_QUEUE_STATUSES = ("pending", "done", "failed", "waiting", "cancelled")
_INPUT_REQUEST_STATUSES = ("open", "answered", "running", "done", "cancelled", "expired")
# Live requests: at most one per queue item.
_LIVE_INPUT_REQUESTS = "status IN ('open', 'answered', 'running')"
_ATTACHMENT_KINDS = ("image", "pdf")
_END_REASONS = ("user", "idle", "unattended")
_REPEAT_RULES = ("daily", "weekdays", "weekends", "weekly")
_FACT_ORIGINS = ("told", "learned")
_FACT_REMOVAL_REASONS = ("forgotten", "replaced", "duplicate")
_FACTS_OUTCOMES = ("done", "gave_up")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


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
        CheckConstraint(_in("end_reason", _END_REASONS), name="end_reason"),
        CheckConstraint(_in("facts_outcome", _FACTS_OUTCOMES), name="facts_outcome"),
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
    # Fact extraction (change add-user-facts): null while pending.
    facts_outcome: Mapped[str | None] = mapped_column(Text)
    facts_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    facts_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Entry(Base):
    __tablename__ = "entries"
    __table_args__ = (
        CheckConstraint(_in("kind", _ENTRY_KINDS), name="kind"),
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


class QueueItem(Base):
    __tablename__ = "queue_items"
    __table_args__ = (
        CheckConstraint(_in("kind", _QUEUE_KINDS), name="kind"),
        CheckConstraint(_in("status", _QUEUE_STATUSES), name="status"),
        Index("ix_queue_items_pending", "id", postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Whose turn it is: the sender, the /ask target or the reminder's recipient.
    user_name: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # For ``reminder`` items: the reminder to deliver.
    reminder_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("reminders.id"))
    # For ``reminder`` items: the due time of the occurrence delivered.
    reminder_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        CheckConstraint(_in("status", _REMINDER_STATUSES), name="status"),
        CheckConstraint(_in("repeat", _REPEAT_RULES), name="repeat"),
        CheckConstraint("repeat_days BETWEEN 1 AND 127", name="repeat_days"),
        # A one-shot reminder has none of the rule; a repeating one all of it.
        CheckConstraint(
            "(repeat IS NULL) = (repeat_days IS NULL) AND (repeat IS NULL) = (repeat_time IS NULL)",
            name="repeat_rule",
        ),
        Index(
            "ix_reminders_pending_due",
            "due_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The repeat rule (null for a one-shot): its name, the weekdays as a bitmask
    # (Monday = bit 0) and the local wall time "HH:MM".
    repeat: Mapped[str | None] = mapped_column(Text)
    repeat_days: Mapped[int | None] = mapped_column(SmallInteger)
    repeat_time: Mapped[str | None] = mapped_column(Text)
    # The reminder turn may answer NO_REPLY.
    quiet_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())


class ReminderRecipient(Base):
    """Who a reminder is for (change add-multiple-users, design D7)."""

    __tablename__ = "reminder_recipients"

    reminder_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("reminders.id"), primary_key=True
    )
    user_name: Mapped[str] = mapped_column(Text, primary_key=True)


class TurnToolCall(Base):
    __tablename__ = "turn_tool_calls"
    __table_args__ = (Index("ix_turn_tool_calls_queue_item_id", "queue_item_id", "seq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    queue_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("queue_items.id"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    tool: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments: Mapped[Any] = mapped_column(JSONB, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    result_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InputRequest(Base):
    __tablename__ = "input_requests"
    __table_args__ = (
        CheckConstraint(_in("status", _INPUT_REQUEST_STATUSES), name="status"),
        # One live request per turn (design D4).
        Index(
            "uq_input_requests_live_per_item",
            "queue_item_id",
            unique=True,
            postgresql_where=text(_LIVE_INPUT_REQUESTS),
        ),
        # The expiry loop and the new-message cancel look for open requests.
        Index("ix_input_requests_open", "user_name", postgresql_where=text("status = 'open'")),
    )

    # In the buttons' callback data.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    queue_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("queue_items.id"), nullable=False
    )
    # Whose turn it is; every query filters on it.
    user_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    # The model-facing call that waits.
    tool: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments: Mapped[Any] = mapped_column(JSONB, nullable=False)
    # The rendered question: title, body, detail, options.
    question: Mapped[Any] = mapped_column(JSONB, nullable=False)
    # The toolset's resume state (for MCP: the key and ``request_state``).
    state: Mapped[Any] = mapped_column(JSONB, nullable=False)
    # The chosen option's answer, once answered.
    answer: Mapped[Any] = mapped_column(JSONB, nullable=True)
    # The turn's saved progress: tail, call, counters, conversation, upto.
    turn: Mapped[Any] = mapped_column(JSONB, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # ``new_message``, ``expired`` or ``not_delivered``.
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint(_in("kind", _ATTACHMENT_KINDS), name="kind"),
        Index("ix_attachments_queue_item_id", "queue_item_id", "position"),
        Index("ix_attachments_entry_id", "entry_id"),
    )

    # The number the model sees in a handle ("#7").
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    queue_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("queue_items.id"), nullable=False
    )
    # Who sent it.
    user_name: Mapped[str] = mapped_column(Text, nullable=False)
    # The user entry of the message, once its turn has recorded it.
    entry_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("entries.id"))
    # Order within the message (an album).
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    file_name: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AttachmentPage(Base):
    __tablename__ = "attachment_pages"

    attachment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("attachments.id"), primary_key=True
    )
    # 1-based, as the model and users count them.
    page: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)


class Fact(Base):
    __tablename__ = "facts"
    __table_args__ = (
        CheckConstraint(_in("origin", _FACT_ORIGINS), name="origin"),
        CheckConstraint(_in("removed_reason", _FACT_REMOVAL_REASONS), name="removed_reason"),
        CheckConstraint("(removed_at IS NULL) = (removed_reason IS NULL)", name="removed"),
        # Every read but an audit is of a user's active facts.
        Index(
            "ix_facts_active",
            "user_name",
            "id",
            postgresql_where=text("removed_at IS NULL"),
        ),
        # Active facts whose duplicate check is pending (change fix-fact-removals).
        Index(
            "ix_facts_duplicate_pending",
            "id",
            postgresql_where=text("removed_at IS NULL AND duplicate_checked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_name: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The conversation a learned fact came from.
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("conversations.id", ondelete="SET NULL")
    )
    embedding: Mapped[Any] = mapped_column(Vector(), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text)
    embedding_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    embedding_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_reason: Mapped[str | None] = mapped_column(Text)
    replaced_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("facts.id"))
    # The model's duplicate check (change fix-fact-removals): null while pending.
    duplicate_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duplicate_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duplicate_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
