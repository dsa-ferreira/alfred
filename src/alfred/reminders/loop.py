"""The due-reminder loop (change add-reminders, design D4).

```
loop forever:
    claim due reminders into the queue   # one transaction, see Reminders.claim_due
    if any: wake the queue worker
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
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from alfred.reminders.store import Reminders

log = logging.getLogger(__name__)


async def claim_once(
    store: Reminders, signal: asyncio.Event, now: Callable[[], datetime]
) -> list[int]:
    """Queue the reminders due now and wake the worker if there were any."""
    ids = await store.claim_due(now())
    if ids:
        log.info("%d due reminder(s) queued as item(s) %s", len(ids), ids)
        signal.set()
    return ids


def seconds_to_next_check(now: datetime, interval_s: float) -> float:
    """Seconds from ``now`` to the next check: the next multiple of the interval within
    the minute, or the next whole minute."""
    into_minute = now.second + now.microsecond / 1_000_000
    step = min(interval_s, 60)
    following = min((math.floor(into_minute / step) + 1) * step, 60)
    return following - into_minute


async def run_due_loop(
    store: Reminders,
    signal: asyncio.Event,
    interval_s: float,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> None:
    log.info("reminder loop started, checking every %gs", interval_s)
    while True:
        try:
            await claim_once(store, signal, now)
        except Exception:
            # A database blip: the reminders stay pending and are claimed next time.
            log.exception("checking for due reminders failed; trying again later")
        # Recomputed each time, so an early or late wake-up never drifts.
        await sleep(seconds_to_next_check(now(), interval_s))
