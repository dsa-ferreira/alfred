"""The background job that summarises and embeds archived conversations.

It finds archived conversations that lack a summary, or an embedding from
the configured model, oldest first, and waits for each one's
``summary_next_at``. This also backfills conversations from before the
upgrade, re-embeds them after a model change, and makes the summary of
every conversation that ends. A conversation that only needs its embedding
(another server) doesn't wait for a quiet queue.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.archive.summaries import Summaries
from alfred.db.models import Conversation


class SummaryJob:
    """Title, summary and embedding of archived conversations: the first background job.

    Summaries feed search, which users see, so it runs before the others.
    """

    uses_model = True

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        summaries: Summaries,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
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
