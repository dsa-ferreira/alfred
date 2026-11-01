"""SQLAlchemy models (design D7).

- ``conversations``: at most one has ``ended_at IS NULL`` (the current one).
- ``entries``: what was said in a conversation, in order. Entries written by
  a turn carry its ``queue_item_id``, so a retried turn records them once.
- ``queue_items``: AI-bound work (owner messages and ``/ask`` prompts).
- ``turn_tool_calls``: the tool calls each turn made, for auditing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names, so migrations can refer to them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

ENTRY_KINDS = ("owner", "assistant", "notice", "ask_prompt", "ask_reply")
QUEUE_KINDS = ("owner", "ask")
QUEUE_STATUSES = ("pending", "done", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        # Only one row may be current: every current row indexes the same key.
        Index(
            "uq_conversations_one_current",
            text("(ended_at IS NULL)"),
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Entry(Base):
    __tablename__ = "entries"
    __table_args__ = (
        CheckConstraint(_in("kind", ENTRY_KINDS), name="kind"),
        Index("ix_entries_conversation_id_id", "conversation_id", "id"),
        # A retried turn must not record its owner message (or reply) twice.
        Index("uq_entries_queue_item_id_kind", "queue_item_id", "kind", unique=True),
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QueueItem(Base):
    __tablename__ = "queue_items"
    __table_args__ = (
        CheckConstraint(_in("kind", QUEUE_KINDS), name="kind"),
        CheckConstraint(_in("status", QUEUE_STATUSES), name="status"),
        Index("ix_queue_items_pending", "id", postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
