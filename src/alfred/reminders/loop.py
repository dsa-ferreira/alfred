"""The due-reminder loop.

```
loop forever:
    claim due reminders into the queue   # one transaction, see Reminders.claim_due
    if any: tell the queue (wakes the worker)
    sleep until the next check on the clock
```

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

from alfred.reminders.store import Reminders
from alfred.times import seconds_to_next_check

log = logging.getLogger(__name__)


async def run_due_loop(
    store: Reminders,
    notify: Callable[[], None],
    interval_s: float,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> None:
    log.info("reminder loop started, checking every %gs", interval_s)
    while True:
        try:
            await _claim_once(store, notify, now)
        except Exception:
            # A database blip: the reminders stay pending and are claimed next time.
            log.exception("checking for due reminders failed; trying again later")
        # Recomputed each time, so an early or late wake-up never drifts.
        await sleep(seconds_to_next_check(now(), interval_s))


async def _claim_once(
    store: Reminders, notify: Callable[[], None], now: Callable[[], datetime]
) -> list[int]:
    """Queue the reminders due now and, if there were any, tell the queue
    (``QueueActivity.notify``: it wakes the worker and the memory sweeper)."""
    ids = await store.claim_due(now())
    if ids:
        log.info("%d due reminder(s) queued as item(s) %s", len(ids), ids)
        notify()
    return ids
