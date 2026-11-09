"""The memory sweeper (memory design D4).

It is its own task, not a queue item, so a summary that keeps failing never
holds up owner messages. It finds archived conversations that lack a
summary, or an embedding from the configured model, oldest first, and waits
for each one's ``summary_next_at``. This also backfills conversations from
before the upgrade and re-embeds them after a model change.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.config import Config
from alfred.db.models import Conversation
from alfred.memory.summaries import Summaries

log = logging.getLogger(__name__)


class Sweeper:
    def __init__(
        self,
        config: Config,
        sessions: async_sessionmaker[AsyncSession],
        summaries: Summaries,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._sessions = sessions
        self.summaries = summaries
        self._now = now

    async def run_forever(self) -> None:
        log.info("memory sweeper started")
        last: int | None = None
        while True:
            self.summaries.changed.clear()  # before reading, so a nudge is never missed
            due = await self._next_due()
            if due is not None and due != last:
                last = due
                await self.summaries.summarise(due)
                continue
            if due is not None:
                # Still due right after it was processed: its retry couldn't be
                # recorded. Don't spin; try again after the shortest delay.
                wait = self._config.queue.retry_initial_s
            else:
                wait = await self._seconds_until_due()
            last = None
            try:
                async with asyncio.timeout(wait):
                    await self.summaries.changed.wait()
            except TimeoutError:
                pass

    async def sweep_once(self) -> float | None:
        """Process the oldest due conversation, or say how many seconds until one is due.

        Returns ``None`` after processing one.
        """
        due = await self._next_due()
        if due is None:
            return await self._seconds_until_due()
        await self.summaries.summarise(due)
        return None

    def _pending(self) -> Any:
        return (
            Conversation.ended_at.is_not(None),
            or_(
                Conversation.summary.is_(None),
                Conversation.embedding.is_(None),
                Conversation.embedding_model.is_distinct_from(self.summaries.embedding_model),
            ),
        )

    async def _next_due(self) -> int | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(Conversation.id)
                .where(
                    *self._pending(),
                    or_(
                        Conversation.summary_next_at.is_(None),
                        Conversation.summary_next_at <= self._now(),
                    ),
                )
                .order_by(Conversation.id)
                .limit(1)
            )

    async def _seconds_until_due(self) -> float:
        async with self._sessions() as session:
            soonest = await session.scalar(
                select(func.min(Conversation.summary_next_at)).where(*self._pending())
            )
        if soonest is None:
            return self._config.queue.retry_max_s
        return max(0.0, (soonest - self._now()).total_seconds())
