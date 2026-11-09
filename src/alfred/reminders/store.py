"""Reminders in Postgres (change add-reminders, design D3, D4).

``claim_due`` marks due reminders ``fired`` and inserts their queue items in
one transaction, so a reminder is queued exactly once: a crash leaves it
either pending (claimed again after restart) or queued (the queue survives
restarts).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.models import QueueItem, Reminder

CancelOutcome = Literal["cancelled", "unknown", "fired", "cancelled_before"]


@dataclass(frozen=True)
class ReminderRow:
    id: int
    text: str
    due_at: datetime
    status: str


def _row(reminder: Reminder) -> ReminderRow:
    return ReminderRow(reminder.id, reminder.text, reminder.due_at, reminder.status)


class Reminders:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def set(self, text: str, due_at: datetime) -> int:
        async with self._sessions.begin() as session:
            reminder = Reminder(text=text, due_at=due_at)
            session.add(reminder)
            await session.flush()
            return reminder.id

    async def get(self, reminder_id: int) -> ReminderRow | None:
        async with self._sessions() as session:
            reminder = await session.get(Reminder, reminder_id)
            return _row(reminder) if reminder is not None else None

    async def pending(self) -> list[ReminderRow]:
        """Reminders set and not yet queued, soonest first."""
        async with self._sessions() as session:
            rows = await session.scalars(
                select(Reminder)
                .where(Reminder.status == "pending")
                .order_by(Reminder.due_at, Reminder.id)
            )
            return [_row(r) for r in rows]

    async def cancel(
        self, reminder_id: int, now: datetime
    ) -> tuple[CancelOutcome, ReminderRow | None]:
        """Cancel a pending reminder. Anything else is left as it is."""
        async with self._sessions.begin() as session:
            reminder = await session.scalar(
                select(Reminder).where(Reminder.id == reminder_id).with_for_update()
            )
            if reminder is None:
                return "unknown", None
            if reminder.status == "fired":
                return "fired", _row(reminder)
            if reminder.status == "cancelled":
                return "cancelled_before", _row(reminder)
            reminder.status = "cancelled"
            reminder.cancelled_at = now
            return "cancelled", _row(reminder)

    async def claim_due(self, now: datetime) -> list[int]:
        """Queue every pending reminder due by ``now``, in due order. Returns the queue item IDs."""
        async with self._sessions.begin() as session:
            due = list(
                await session.scalars(
                    select(Reminder)
                    .where(Reminder.status == "pending", Reminder.due_at <= now)
                    .order_by(Reminder.due_at, Reminder.id)
                    .with_for_update(skip_locked=True)
                )
            )
            ids: list[int] = []
            for reminder in due:
                reminder.status = "fired"
                reminder.fired_at = now
                item = QueueItem(kind="reminder", text=reminder.text, reminder_id=reminder.id)
                session.add(item)
                # One by one, so queue item IDs (the queue's order) follow due order.
                await session.flush()
                ids.append(item.id)
            return ids
