"""The memory sweeper (memory design D4).

It is its own task, not a queue item, so a summary that keeps failing never
holds up users' messages. It finds archived conversations that lack a
summary, or an embedding from the configured model, oldest first, and waits
for each one's ``summary_next_at``. This also backfills conversations from
before the upgrade, re-embeds them after a model change, and makes the
summary of every conversation that ends.

Summaries give way to the queue (change close-idle-conversations, design D5,
D6): the model server runs one request at a time, so a summary request
starts only when no queue item is waiting or running and none has finished
within ``[memory] summary_quiet_s``, and an item that arrives while it runs
cancels it. A conversation that only needs its embedding (another server)
doesn't wait.

The sweeper runs a list of jobs in priority order (change add-user-facts,
design D2): the conversation summaries first, then whatever else was given
(fact extraction, fact embedding). Each round it runs the first job with
work due; a job that ``uses_model`` is only offered model work while the
queue is quiet, and is cancelled by an arriving item. With nothing due it
sleeps until the soonest job is due, the queue goes quiet, or a job's
``changed`` is set.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Hashable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.config import Config
from alfred.db.models import Conversation
from alfred.memory.summaries import Summaries

if TYPE_CHECKING:
    from alfred.queue import QueueActivity

log = logging.getLogger(__name__)


class _QueueState(Protocol):
    """What the sweeper needs to know about the queue (``alfred.queue.Queue``)."""

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


class _ConversationSummaryJob:
    """Title, summary and embedding of archived conversations (memory design D4)."""

    uses_model = True

    def __init__(
        self,
        config: Config,
        sessions: async_sessionmaker[AsyncSession],
        summaries: Summaries,
        now: Callable[[], datetime],
    ) -> None:
        self._config = config
        self._sessions = sessions
        self.summaries = summaries
        self._now = now

    @property
    def changed(self) -> asyncio.Event:
        return self.summaries.changed

    async def next_due(self, model_allowed: bool) -> int | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(Conversation.id)
                .where(
                    *self._pending(embedding_only=not model_allowed),
                    or_(
                        Conversation.summary_next_at.is_(None),
                        Conversation.summary_next_at <= self._now(),
                    ),
                )
                .order_by(Conversation.id)
                .limit(1)
            )

    async def seconds_until_due(self, model_allowed: bool) -> float | None:
        async with self._sessions() as session:
            soonest = await session.scalar(
                select(func.min(Conversation.summary_next_at)).where(
                    *self._pending(embedding_only=not model_allowed)
                )
            )
        if soonest is None:
            return None
        return max(0.0, (soonest - self._now()).total_seconds())

    async def run(self, key: int, preempt: asyncio.Event | None) -> None:
        await self.summaries.summarise(key, preempt=preempt)

    def _pending(self, *, embedding_only: bool) -> Any:
        conditions: tuple[Any, ...] = (
            Conversation.ended_at.is_not(None),
            or_(
                Conversation.summary.is_(None),
                Conversation.embedding.is_(None),
                Conversation.embedding_model.is_distinct_from(self.summaries.embedding_model),
            ),
        )
        if embedding_only:
            # Already summarised: only the embedding request (never preempted) is left.
            conditions += (Conversation.summary.is_not(None),)
        return conditions


class Sweeper:
    def __init__(
        self,
        config: Config,
        sessions: async_sessionmaker[AsyncSession],
        summaries: Summaries,
        *,
        queue: _QueueState | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        jobs: Sequence[_Job] = (),
    ) -> None:
        self._config = config
        self.summaries = summaries
        # None: no queue to give way to (tests), so always quiet.
        self._queue = queue
        # In priority order: summaries feed search, which users see.
        self.jobs: list[_Job] = [_ConversationSummaryJob(config, sessions, summaries, now), *jobs]

    async def run_forever(self) -> None:
        log.info("memory sweeper started")
        # Per job: the key it last ran, until the sweeper next sleeps.
        last: dict[int, Hashable] = {}
        while True:
            # Cleared before reading, so a nudge or an arrival is never missed.
            for job in self.jobs:
                job.changed.clear()
            arrived = None
            if self._queue is not None:
                self._queue.activity.changed.clear()
                # Before the quiet check: an item that arrives after this sets
                # it again, and one queued before is seen as pending.
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
                    waits.append(self._config.queue.retry_initial_s)
                    continue
                seconds = await job.seconds_until_due(model_allowed)
                if seconds is not None:
                    waits.append(seconds)
            wait = min(waits, default=self._config.queue.retry_max_s)
            if not stuck and quiet_in is not None and quiet_in > 0:
                wait = min(wait, quiet_in)
            last = {}
            events = [job.changed for job in self.jobs]
            if self._queue is not None:
                events.append(self._queue.activity.changed)
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
        return min(waits, default=self._config.queue.retry_max_s)

    async def _quiet_in(self) -> float | None:
        """Seconds until a model request may start: 0 now, ``None`` while an item is pending."""
        if self._queue is None:
            return 0.0
        if await self._queue.has_pending():
            return None
        left = self._config.memory.summary_quiet_s - self._queue.activity.quiet_for()
        return max(0.0, left)


async def _wait_any(events: list[asyncio.Event], seconds: float) -> None:
    """Until one of ``events`` is set, or ``seconds`` pass."""
    waiters = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
