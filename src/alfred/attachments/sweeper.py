"""Deleting orphan files from the bucket (change add-attachments, design D10).

A file is uploaded before its row is committed (design D2), so a crash in
between leaves an object no row refers to. Once at startup and then every
24 hours, every object under ``attachments/`` older than a day with no
``attachments`` row is deleted. The day leaves any upload in progress far
behind. Errors are logged and the round is tried again next time; the task
never stops alfred, and no turn waits for it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.attachments.store import PREFIX, ObjectStore, StoreError
from alfred.db.models import Attachment

log = logging.getLogger(__name__)

INTERVAL_S = 24 * 3600
MIN_AGE = timedelta(days=1)
_BATCH = 500  # keys per database lookup


class OrphanSweeper:
    def __init__(
        self,
        store: ObjectStore,
        sessions: async_sessionmaker[AsyncSession],
        *,
        interval_s: float = INTERVAL_S,
        min_age: timedelta = MIN_AGE,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._sessions = sessions
        self._interval_s = interval_s
        self._min_age = min_age
        self._now = now

    async def run_forever(self) -> None:
        log.info("attachment sweeper started")
        while True:
            try:
                await self.sweep_once()
            except StoreError as exc:  # already logged by the store, without secrets
                log.warning("orphan sweep failed; next round in %gs: %s", self._interval_s, exc)
            except Exception:
                log.exception("orphan sweep failed; next round in %gs", self._interval_s)
            await asyncio.sleep(self._interval_s)

    async def sweep_once(self) -> int:
        """Delete the old objects no row refers to. Returns how many."""
        cutoff = self._now() - self._min_age
        old = [o.key async for o in self._store.list(PREFIX) if o.last_modified < cutoff]
        deleted = 0
        for start in range(0, len(old), _BATCH):
            batch = old[start : start + _BATCH]
            async with self._sessions() as session:
                known = set(
                    await session.scalars(
                        select(Attachment.object_key).where(Attachment.object_key.in_(batch))
                    )
                )
            for key in batch:
                if key not in known:
                    await self._store.delete(key)
                    log.info("deleted orphan object %s", key)
                    deleted += 1
        if deleted or old:
            log.info("orphan sweep: %d old object(s), %d deleted", len(old), deleted)
        return deleted
