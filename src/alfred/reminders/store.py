"""Reminders in Postgres (change add-reminders, design D3, D4).

A reminder has a creator and one or more recipients (change
add-multiple-users, design D7). Listing and cancelling are for the users
involved, its creator or a recipient; to anyone else the reminder doesn't
exist. Cancelling cancels it for every recipient.

``claim_due`` marks due reminders ``fired`` and inserts one queue item per
recipient in one transaction, so each recipient's item is queued exactly
once: a crash leaves the reminder either pending (claimed again after
restart) or queued (the queue survives restarts).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.models import QueueItem, Reminder, ReminderRecipient

CancelOutcome = Literal["cancelled", "unknown", "fired", "cancelled_before"]


@dataclass(frozen=True)
class ReminderRow:
    id: int
    text: str
    due_at: datetime
    status: str
    created_by: str
    # In name order.
    recipients: tuple[str, ...]

    def only_for(self, user: str) -> bool:
        """Set by ``user`` for ``user`` alone."""
        return self.created_by == user and self.recipients == (user,)


async def _rows(session: AsyncSession, reminders: Sequence[Reminder]) -> list[ReminderRow]:
    """The reminders with their recipients, in the given order (one query)."""
    ids = [r.id for r in reminders]
    recipients: dict[int, list[str]] = {i: [] for i in ids}
    if ids:
        found = await session.execute(
            select(ReminderRecipient.reminder_id, ReminderRecipient.user_name)
            .where(ReminderRecipient.reminder_id.in_(ids))
            .order_by(ReminderRecipient.user_name)
        )
        for reminder_id, name in found:
            recipients[reminder_id].append(name)
    return [
        ReminderRow(r.id, r.text, r.due_at, r.status, r.created_by, tuple(recipients[r.id]))
        for r in reminders
    ]


def _involves(user: str):  # noqa: ANN202 - a SQLAlchemy condition
    """The reminder was set by ``user`` or is for them."""
    return or_(
        Reminder.created_by == user,
        exists().where(
            ReminderRecipient.reminder_id == Reminder.id, ReminderRecipient.user_name == user
        ),
    )


class Reminders:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def set(
        self, text: str, due_at: datetime, created_by: str, recipients: Sequence[str]
    ) -> int:
        """Store a reminder ``created_by`` set for ``recipients`` (at least one)."""
        names = list(dict.fromkeys(recipients))
        if not names:
            raise ValueError("a reminder needs at least one recipient")
        async with self._sessions.begin() as session:
            reminder = Reminder(text=text, due_at=due_at, created_by=created_by)
            session.add(reminder)
            await session.flush()
            session.add_all(ReminderRecipient(reminder_id=reminder.id, user_name=n) for n in names)
            return reminder.id

    async def get(self, reminder_id: int) -> ReminderRow | None:
        async with self._sessions() as session:
            reminder = await session.get(Reminder, reminder_id)
            return (await _rows(session, [reminder]))[0] if reminder is not None else None

    async def pending(self, user: str) -> list[ReminderRow]:
        """Reminders involving ``user``, set and not yet queued, soonest first."""
        async with self._sessions() as session:
            found = await session.scalars(
                select(Reminder)
                .where(Reminder.status == "pending", _involves(user))
                .order_by(Reminder.due_at, Reminder.id)
            )
            return await _rows(session, list(found))

    async def cancel(
        self, reminder_id: int, now: datetime, user: str
    ) -> tuple[CancelOutcome, ReminderRow | None]:
        """Cancel a pending reminder involving ``user``, for everyone it is for.

        One that doesn't involve ``user`` is ``unknown``, as if it didn't
        exist. Anything else is left as it is.
        """
        async with self._sessions.begin() as session:
            reminder = await session.scalar(
                select(Reminder)
                .where(Reminder.id == reminder_id, _involves(user))
                .with_for_update(of=Reminder)
            )
            if reminder is None:
                return "unknown", None
            if reminder.status == "fired":
                return "fired", (await _rows(session, [reminder]))[0]
            if reminder.status == "cancelled":
                return "cancelled_before", (await _rows(session, [reminder]))[0]
            reminder.status = "cancelled"
            reminder.cancelled_at = now
            return "cancelled", (await _rows(session, [reminder]))[0]

    async def claim_due(self, now: datetime) -> list[int]:
        """Queue every pending reminder due by ``now``, one item per recipient.

        In due order, then recipient name. Returns the queue item IDs.
        """
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
            for row in await _rows(session, due):
                reminder = next(r for r in due if r.id == row.id)
                reminder.status = "fired"
                reminder.fired_at = now
                for name in row.recipients:
                    item = QueueItem(
                        kind="reminder", user_name=name, text=row.text, reminder_id=row.id
                    )
                    session.add(item)
                    # One by one, so queue item IDs (the queue's order) follow due order.
                    await session.flush()
                    ids.append(item.id)
            return ids
