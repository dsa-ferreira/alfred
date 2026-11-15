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
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
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


class QueueState(Protocol):
    """What the sweeper needs to know about the queue (``alfred.queue.Queue``)."""

    activity: QueueActivity

    async def has_pending(self) -> bool: ...


class Sweeper:
    def __init__(
        self,
        config: Config,
        sessions: async_sessionmaker[AsyncSession],
        summaries: Summaries,
        *,
        queue: QueueState | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._sessions = sessions
        self.summaries = summaries
        # None: no queue to give way to (tests), so always quiet.
        self._queue = queue
        self._now = now

    async def run_forever(self) -> None:
        log.info("memory sweeper started")
        last: int | None = None
        while True:
            # Cleared before reading, so a nudge or an arrival is never missed.
            self.summaries.changed.clear()
            arrived = None
            if self._queue is not None:
                self._queue.activity.changed.clear()
                # Before the quiet check: an item that arrives after this sets
                # it again, and one queued before is seen as pending.
                self._queue.activity.arrived.clear()
                arrived = self._queue.activity.arrived
            quiet_in = await self._quiet_in()
            embedding_only = quiet_in != 0
            due = await self._next_due(embedding_only=embedding_only)
            if due is not None and due != last:
                last = due
                await self.summaries.summarise(due, preempt=arrived)
                if arrived is not None and arrived.is_set():
                    # Preempted (or an item came during the embedding): not a
                    # failed retry write. Wait for a quiet queue again.
                    last = None
                continue
            if due is not None:
                # Still due right after it was processed: its retry couldn't be
                # recorded. Don't spin; try again after the shortest delay.
                wait = self._config.queue.retry_initial_s
            else:
                wait = await self._seconds_until_due(embedding_only=embedding_only)
                if quiet_in is not None and quiet_in > 0:
                    wait = min(wait, quiet_in)
            last = None
            events = [self.summaries.changed]
            if self._queue is not None:
                events.append(self._queue.activity.changed)
            await _wait_any(events, wait)

    async def sweep_once(self) -> float | None:
        """Process the oldest due conversation, or say how many seconds until one is due.

        Returns ``None`` after processing one. Doesn't wait for a quiet queue.
        """
        due = await self._next_due()
        if due is None:
            return await self._seconds_until_due()
        await self.summaries.summarise(due)
        return None

    async def _quiet_in(self) -> float | None:
        """Seconds until a summary may start: 0 now, ``None`` while an item is pending."""
        if self._queue is None:
            return 0.0
        if await self._queue.has_pending():
            return None
        left = self._config.memory.summary_quiet_s - self._queue.activity.quiet_for()
        return max(0.0, left)

    def _pending(self, *, embedding_only: bool = False) -> Any:
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

    async def _next_due(self, *, embedding_only: bool = False) -> int | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(Conversation.id)
                .where(
                    *self._pending(embedding_only=embedding_only),
                    or_(
                        Conversation.summary_next_at.is_(None),
                        Conversation.summary_next_at <= self._now(),
                    ),
                )
                .order_by(Conversation.id)
                .limit(1)
            )

    async def _seconds_until_due(self, *, embedding_only: bool = False) -> float:
        async with self._sessions() as session:
            soonest = await session.scalar(
                select(func.min(Conversation.summary_next_at)).where(
                    *self._pending(embedding_only=embedding_only)
                )
            )
        if soonest is None:
            return self._config.queue.retry_max_s
        return max(0.0, (soonest - self._now()).total_seconds())


async def _wait_any(events: list[asyncio.Event], seconds: float) -> None:
    """Until one of ``events`` is set, or ``seconds`` pass."""
    waiters = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
