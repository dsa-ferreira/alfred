"""Closing idle conversations.

With ``[history] idle_close_after_h`` set, a user's current conversation is
archived, and a new one started, once that long has passed since the user's
last own message in it. Notices, ``/ask`` turns and reminders don't count,
and a conversation with no message from its user is never closed. Nothing is
sent at that moment: the user's next message gets the marker
(``Worker._process``).

```
loop forever:
    close every current conversation whose user last wrote before now - idle time
    sleep until the soonest one is due (at most the idle time; at most 60 s
    after a candidate was skipped because its user had a queue item)
```

Everything it needs is in the database, so it survives restarts with no
timers. A new message only pushes a due time later, and a conversation that
gets its first message is due no sooner than the idle time from now, the
longest sleep, so no wake-up event is needed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from alfred.features.conversations.store import Busy, History

log = logging.getLogger(__name__)

# Longest sleep after a candidate couldn't be closed (a queue item, a new message).
_SKIPPED_RETRY_S = 60.0


class IdleCloser:
    def __init__(
        self,
        history: History,
        after: timedelta,
        *,
        busy: Busy,
        nudge: Callable[[], None],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._history = history
        self._after = after
        # Whether a user's queue item is waiting or running: theirs stays open.
        self._busy = busy
        # Wakes the memory sweeper to summarise what was closed.
        self._nudge = nudge
        self._now = now
        self._sleep = sleep

    async def run_forever(self) -> None:
        log.info(
            "idle close started: conversations close %s after the user's last message", self._after
        )
        while True:
            try:
                wait = await self._close_once()
            except Exception:
                # A database blip: nothing changed, look again soon.
                log.exception("closing idle conversations failed; trying again later")
                wait = _SKIPPED_RETRY_S
            await self._sleep(wait)

    async def _close_once(self) -> float:
        """Close what is due. Returns the seconds to sleep until the next pass."""
        now = self._now()
        cutoff = now - self._after
        soonest: datetime | None = None
        skipped = False
        for last in await self._history.last_user_messages():
            if last.at > cutoff:
                due = last.at + self._after
                soonest = due if soonest is None else min(soonest, due)
                continue
            reset = await self._history.close_idle(last.conversation_id, cutoff, busy=self._busy)
            if reset is None:
                # A queue item of theirs is waiting or running, or they just wrote.
                skipped = True
                continue
            log.info(
                "%s's conversation %s closed after inactivity (last message %s), %s started",
                last.user,
                reset.archived_id,
                last.at.isoformat(timespec="seconds"),
                reset.new_id,
            )
            self._nudge()
        wait = (
            (soonest - now).total_seconds() if soonest is not None else self._after.total_seconds()
        )
        if skipped:
            wait = min(wait, _SKIPPED_RETRY_S)
        return max(0.0, wait)
