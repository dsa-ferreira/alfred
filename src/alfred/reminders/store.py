"""Reminders in Postgres.

A reminder has a creator and one or more recipients. Listing and
cancelling are for the users involved, its creator or a recipient; to
anyone else the reminder doesn't exist. The creator cancels it for every
recipient; a recipient who didn't set it only leaves it, and the last one
leaving cancels it.

``claim_due`` marks due reminders ``fired`` in the caller's transaction
and returns their occurrences; ``turns.due_reminders`` queues one item per
recipient in that same transaction, so each recipient's item is queued
exactly once: a crash leaves the reminder either pending (claimed again
after restart) or queued (the queue survives restarts). A repeating
reminder stays ``pending`` and moves on to its next occurrence instead;
after an outage only its latest missed occurrence is claimed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.reminders.tables import Reminder, ReminderRecipient
from alfred.reminders.when import Rule, latest_occurrence, next_occurrence

_UTC_ZONE = ZoneInfo("UTC")

# ``left``: a recipient who didn't set it was removed; it stays set for the others.
_CancelOutcome = Literal["cancelled", "left", "unknown", "fired", "cancelled_before"]


@dataclass(frozen=True)
class ReminderRow:
    id: int
    text: str
    due_at: datetime
    status: str
    created_by: str
    # In name order.
    recipients: tuple[str, ...]
    # The repeat rule; None for a one-shot reminder.
    rule: Rule | None = None
    # The reminder turn may answer NO_REPLY.
    quiet_ok: bool = False

    def only_for(self, user: str) -> bool:
        """Set by ``user`` for ``user`` alone."""
        return self.created_by == user and self.recipients == (user,)


class Reminders:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], zone: ZoneInfo = _UTC_ZONE
    ) -> None:
        self._sessions = sessions
        # Where repeating reminders' wall times are (alfred's timezone).
        self._zone = zone

    async def set(
        self,
        text: str,
        due_at: datetime,
        created_by: str,
        recipients: Sequence[str],
        *,
        rule: Rule | None = None,
        quiet_ok: bool = False,
    ) -> int:
        """Store a reminder ``created_by`` set for ``recipients`` (at least one).

        With a ``rule``, ``due_at`` is its first occurrence.
        """
        names = list(dict.fromkeys(recipients))
        if not names:
            raise ValueError("a reminder needs at least one recipient")
        async with self._sessions.begin() as session:
            reminder = Reminder(
                text=text,
                due_at=due_at,
                created_by=created_by,
                repeat=rule.repeat if rule else None,
                repeat_days=rule.days if rule else None,
                repeat_time=rule.clock_text if rule else None,
                quiet_ok=quiet_ok,
            )
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
    ) -> tuple[_CancelOutcome, ReminderRow | None]:
        """Cancel a pending reminder involving ``user``.

        Its creator cancels it for everyone. A recipient who didn't set it is
        removed from it (``left``; the row's recipients are those remaining),
        and when none remain it is cancelled. Items already queued are left
        alone. One that doesn't involve ``user`` is ``unknown``, as if it
        didn't exist. Anything else is left as it is.
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
            if reminder.created_by != user:
                await session.execute(
                    delete(ReminderRecipient).where(
                        ReminderRecipient.reminder_id == reminder.id,
                        ReminderRecipient.user_name == user,
                    )
                )
                row = (await _rows(session, [reminder]))[0]
                if row.recipients:
                    return "left", row
                # The last recipient left: nobody is reminded any more.
                reminder.status = "cancelled"
                reminder.cancelled_at = now
                return "cancelled", row
            reminder.status = "cancelled"
            reminder.cancelled_at = now
            return "cancelled", (await _rows(session, [reminder]))[0]

    async def claim_due(self, session: AsyncSession, now: datetime) -> list[_Occurrence]:
        """Claim every pending reminder due by ``now``, in the caller's transaction.

        In due order. A one-shot reminder is marked ``fired``; a repeating
        one returns its latest occurrence by ``now`` and moves on to its next
        one after ``now``. Recipients are in name order.
        """
        due = list(
            await session.scalars(
                select(Reminder)
                .where(Reminder.status == "pending", Reminder.due_at <= now)
                .order_by(Reminder.due_at, Reminder.id)
                .with_for_update(skip_locked=True)
            )
        )
        claimed: list[_Occurrence] = []
        for row in await _rows(session, due):
            reminder = next(r for r in due if r.id == row.id)
            reminder.fired_at = now
            occurrence = reminder.due_at
            if row.rule is None:
                reminder.status = "fired"
            else:
                days, clock = row.rule.days, row.rule.clock
                occurrence = latest_occurrence(days, clock, reminder.due_at, now, self._zone)
                reminder.due_at = next_occurrence(days, clock, now, self._zone)
            claimed.append(_Occurrence(row, occurrence))
        await session.flush()
        return claimed


@dataclass(frozen=True)
class _Occurrence:
    """A due reminder, claimed: deliver it to each of its recipients."""

    reminder: ReminderRow
    # When this occurrence was due (a repeating reminder's row has moved on).
    due_at: datetime


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
        ReminderRow(
            r.id,
            r.text,
            r.due_at,
            r.status,
            r.created_by,
            tuple(recipients[r.id]),
            _rule(r),
            r.quiet_ok,
        )
        for r in reminders
    ]


def _rule(reminder: Reminder) -> Rule | None:
    if reminder.repeat is None or reminder.repeat_days is None or reminder.repeat_time is None:
        return None
    return Rule.stored(reminder.repeat, reminder.repeat_days, reminder.repeat_time)


def _involves(user: str):  # noqa: ANN202 - a SQLAlchemy condition
    """The reminder was set by ``user`` or is for them."""
    return or_(
        Reminder.created_by == user,
        exists().where(
            ReminderRecipient.reminder_id == Reminder.id, ReminderRecipient.user_name == user
        ),
    )
