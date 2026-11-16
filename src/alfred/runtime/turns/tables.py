"""The turns tables.

- ``queue_items``: AI-bound work (users' messages, ``/ask`` prompts and due
  reminders), each for one user. What each kind carries is its ``payload``
  (``turns.kinds``).
- ``turn_tool_calls``: the tool calls each turn made, for auditing.
- ``input_requests``: a parked turn's question to its user and the turn's
  saved progress. Its queue item is ``waiting`` while the request is
  ``open``; at most one request per item is live (``open``, ``answered`` or
  ``running``).
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
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from alfred.infra.db.base import Base, in_values

_QUEUE_KINDS = ("chat", "ask", "reminder")
# ``waiting``: parked for its user's answer; ``cancelled``: ended without one.
_QUEUE_STATUSES = ("pending", "done", "failed", "waiting", "cancelled")
_INPUT_REQUEST_STATUSES = ("open", "answered", "running", "done", "cancelled", "expired")
# Live requests: at most one per queue item.
_LIVE_INPUT_REQUESTS = "status IN ('open', 'answered', 'running')"


class QueueItem(Base):
    __tablename__ = "queue_items"
    __table_args__ = (
        CheckConstraint(in_values("kind", _QUEUE_KINDS), name="kind"),
        CheckConstraint(in_values("status", _QUEUE_STATUSES), name="status"),
        Index("ix_queue_items_pending", "id", postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Whose turn it is: the sender, the /ask target or the reminder's recipient.
    user_name: Mapped[str] = mapped_column(Text, nullable=False)
    # The kind's data (``turns.kinds``).
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
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


class InputRequest(Base):
    __tablename__ = "input_requests"
    __table_args__ = (
        CheckConstraint(in_values("status", _INPUT_REQUEST_STATUSES), name="status"),
        # One live request per turn.
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
