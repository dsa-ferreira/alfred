"""The background job runner (the "memory sweeper").

It is its own task, not a queue item, so background work that keeps
failing never holds up users' messages. ``serve`` gives it the jobs, in
priority order: the conversation summaries first (``alfred.features.archive``), then
fact extraction, fact embedding and the duplicate check (``alfred.features.facts``).

Model work gives way to the queue: the model server runs one request at a
time, so a job that ``uses_model`` is only offered model work when no queue
item is waiting or running and none has finished within ``quiet_s``
(``[memory] summary_quiet_s``), and an item that arrives while it runs
cancels it. Each round it runs the first job with work due. With nothing
due it sleeps until the soonest job is due, the queue goes quiet, or a
job's ``changed`` is set.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Hashable, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from alfred.infra.backoff import RetryDelays

if TYPE_CHECKING:
    from alfred.runtime.turns.queue import QueueActivity

log = logging.getLogger(__name__)


class Sweeper:
    def __init__(
        self, jobs: Sequence[_Job], *, queue: _QueueState, quiet_s: float, retry: RetryDelays
    ) -> None:
        # In priority order.
        self.jobs = list(jobs)
        self._queue = queue
        self._quiet_s = quiet_s
        self._retry = retry

    async def run_forever(self) -> None:
        log.info("memory sweeper started")
        # Per job: the key it last ran, until the sweeper next sleeps.
        last: dict[int, Hashable] = {}
        while True:
            # Cleared before reading, so a nudge or an arrival is never missed.
            for job in self.jobs:
                job.changed.clear()
            self._queue.activity.changed.clear()
            # Before the quiet check: an item that arrives after this sets it
            # again, and one queued before is seen as pending.
            self._queue.activity.arrived.clear()
            arrived = self._queue.activity.arrived
            quiet_in = await self._quiet_in()
            model_allowed = quiet_in == 0

            ran = False
            stuck: set[int] = set()
            for index, job in enumerate(self.jobs):
                due = await job.next_due(model_allowed)
                if due is None:
                    continue
                if index in last and last[index] == due:
                    # Still due right after it was processed: its retry couldn't
                    # be recorded. Don't spin; let the other jobs run.
                    stuck.add(index)
                    continue
                last[index] = due
                await job.run(due, arrived if job.uses_model else None)
                if job.uses_model and arrived is not None and arrived.is_set():
                    # Preempted (or an item came meanwhile): not a failed retry
                    # write. Wait for a quiet queue again.
                    last.pop(index, None)
                ran = True
                break
            if ran:
                continue

            waits: list[float] = []
            for index, job in enumerate(self.jobs):
                if index in stuck:
                    # Try again after the shortest delay.
                    waits.append(self._retry.retry_initial_s)
                    continue
                seconds = await job.seconds_until_due(model_allowed)
                if seconds is not None:
                    waits.append(seconds)
            wait = min(waits, default=self._retry.retry_max_s)
            if not stuck and quiet_in is not None and quiet_in > 0:
                wait = min(wait, quiet_in)
            last = {}
            events = [*(job.changed for job in self.jobs), self._queue.activity.changed]
            await _wait_any(events, wait)

    async def _sweep_once(self) -> float | None:
        """Do the first due piece of work, or say how many seconds until one is due.

        Returns ``None`` after doing one. Doesn't wait for a quiet queue.
        """
        for job in self.jobs:
            due = await job.next_due(True)
            if due is not None:
                await job.run(due, None)
                return None
        waits = [s for job in self.jobs if (s := await job.seconds_until_due(True)) is not None]
        return min(waits, default=self._retry.retry_max_s)

    async def _quiet_in(self) -> float | None:
        """Seconds until a model request may start: 0 now, ``None`` while an item is pending."""
        if await self._queue.has_pending():
            return None
        left = self._quiet_s - self._queue.activity.quiet_for()
        return max(0.0, left)


class _QueueState(Protocol):
    """What the sweeper needs to know about the queue (``alfred.runtime.turns.queue.Queue``)."""

    activity: QueueActivity

    async def has_pending(self) -> bool: ...


class _Job(Protocol):
    """One kind of background work the sweeper does."""

    # Whether it sends model requests: they get ``preempt``.
    uses_model: bool
    # Set when what is due may have changed, so the sweeper looks again.
    changed: asyncio.Event

    async def next_due(self, model_allowed: bool) -> Hashable | None:
        """The next piece of work due now, or ``None``.

        ``model_allowed`` is false while the queue isn't quiet: then only
        work that needs no model request may be returned.
        """
        ...

    async def seconds_until_due(self, model_allowed: bool) -> float | None:
        """Until the next piece of work is due, or ``None`` if none is scheduled."""
        ...

    async def run(self, key: Any, preempt: asyncio.Event | None) -> None:
        """Do one piece of work. Never raises; schedules its own retries."""
        ...


async def _wait_any(events: list[asyncio.Event], seconds: float) -> None:
    """Until one of ``events`` is set, or ``seconds`` pass."""
    waiters = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
