"""The due-reminder loop: claimed reminders become queue items.

```
loop forever:
    begin: claim due reminders (reminders), one queue item per recipient (queue)
    if any: tell the queue (wakes the worker)
    sleep until the next check on the clock
```

Claiming and queuing are one transaction, so a claimed reminder is never
lost and never queued twice. Queue item IDs (the queue's order) follow due
order, then recipient name.

Checks run on the wall clock, at every whole minute and every
``[reminders] check_interval_s`` within it (``:00`` and ``:30`` for 30).
Due times are whole minutes, so a reminder is queued at its ``:00``.

The first pass runs at startup, which is what sends reminders missed while
alfred was down, however late. Checking only reads the database; it never
calls the AI.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.reminders.store import Reminders
from alfred.times import seconds_to_next_check
from alfred.turns.kinds.reminder import ReminderPayload
from alfred.turns.queue import Queue

log = logging.getLogger(__name__)


async def run_due_loop(
    sessions: async_sessionmaker[AsyncSession],
    reminders: Reminders,
    queue: Queue,
    interval_s: float,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> None:
    log.info("reminder loop started, checking every %gs", interval_s)
    while True:
        try:
            await _claim_once(sessions, reminders, queue, now())
        except Exception:
            # A database blip: the reminders stay pending and are claimed next time.
            log.exception("checking for due reminders failed; trying again later")
        # Recomputed each time, so an early or late wake-up never drifts.
        await sleep(seconds_to_next_check(now(), interval_s))


async def _claim_once(
    sessions: async_sessionmaker[AsyncSession], reminders: Reminders, queue: Queue, now: datetime
) -> list[int]:
    """Queue the reminders due by ``now`` and, if there were any, tell the queue
    (it wakes the worker and the memory sweeper). Returns the queue item IDs."""
    ids: list[int] = []
    async with sessions.begin() as session:
        for occurrence in await reminders.claim_due(session, now):
            reminder = occurrence.reminder
            # A snapshot: what the reminder says never changes once it is set.
            payload = ReminderPayload(
                reminder.id,
                occurrence.due_at,
                reminder.text,
                reminder.created_by,
                reminder.rule.label() if reminder.rule else None,
                reminder.quiet_ok,
            )
            for name in reminder.recipients:
                ids.append(await queue.add(session, name, payload))
    if ids:
        log.info("%d due reminder(s) queued as item(s) %s", len(ids), ids)
        queue.notify()
    return ids
