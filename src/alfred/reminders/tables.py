"""The reminders tables.

- ``reminders``: timed reminders a user set, for the users in
  ``reminder_recipients``. ``fired`` means handed to the queue, one item
  per recipient. A repeating one (``repeat`` set) stays ``pending`` and
  its ``due_at`` moves to the next occurrence.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from alfred.db.base import Base, in_values

_REMINDER_STATUSES = ("pending", "fired", "cancelled")
_REPEAT_RULES = ("daily", "weekdays", "weekends", "weekly")


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        CheckConstraint(in_values("status", _REMINDER_STATUSES), name="status"),
        CheckConstraint(in_values("repeat", _REPEAT_RULES), name="repeat"),
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
    """Who a reminder is for."""

    __tablename__ = "reminder_recipients"

    reminder_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("reminders.id"), primary_key=True
    )
    user_name: Mapped[str] = mapped_column(Text, primary_key=True)
