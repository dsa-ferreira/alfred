"""The AI-bound queue: users' messages, ``/ask`` prompts and due reminders.

Each is a row in ``queue_items``, for one user. The worker
(``alfred.turns.worker``) takes the oldest ``pending`` row and runs its turn;
nothing behind it starts until it is done or failed. Pending rows simply
resume after a restart. A parked turn's item is ``waiting``, which ``head``
skips; once answered it is ``pending`` again and, being older than anything
queued since, it is the next head.

The queue tells the worker and the memory sweeper when items arrive and
finish (``QueueActivity``). Writes that queue an item with other slices'
rows (``turns.intake``, ``turns.due_reminders``) call :meth:`Queue.add` in
their own transaction, then :meth:`Queue.notify`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.turns.tables import QueueItem

log = logging.getLogger(__name__)

TurnKind = Literal["chat", "ask", "reminder"]

# How queue_items.kind stores a chat message: "user", its name from before
# turns had kinds. Only this module knows; the rest of alfred says "chat".
_STORED_CHAT = "user"


class QueueActivity:
    """When items arrive and finish, for the memory sweeper.

    In-process, like the worker's ``signal``: the queue and the sweeper share
    a process, and "finished recently" needn't survive a restart.
    """

    def __init__(self, signal: asyncio.Event, clock: Callable[[], float] = time.monotonic) -> None:
        self._signal = signal
        self._clock = clock
        # Set on every arrival; a summary request in progress gives way to it.
        self.arrived = asyncio.Event()
        # Set when an item finishes, so a waiting sweeper looks again.
        self.changed = asyncio.Event()
        # Starts at process start: after a restart summaries wait one quiet period too.
        self.last_finished = clock()

    def notify(self) -> None:
        """An item was queued: wake the worker, and tell the sweeper."""
        self._signal.set()
        self.arrived.set()

    def finished(self) -> None:
        """The worker is done with an item, whatever the outcome."""
        self.last_finished = self._clock()
        self.changed.set()

    def quiet_for(self) -> float:
        """Seconds since an item last finished (or since start)."""
        return self._clock() - self.last_finished


class Queue:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessions = sessions
        self.signal = asyncio.Event()
        self.activity = QueueActivity(self.signal, clock)

    async def has_pending(self) -> bool:
        """Whether an item is waiting or running (it stays pending until it ends)."""
        async with self._sessions() as session:
            found = await session.scalar(
                select(QueueItem.id).where(QueueItem.status == "pending").limit(1)
            )
        return found is not None

    async def add(
        self,
        session: AsyncSession,
        user: str,
        kind: TurnKind,
        *,
        text: str,
        source: str | None = None,
        telegram_message_id: int | None = None,
        reminder_id: int | None = None,
        reminder_due_at: datetime | None = None,
    ) -> int:
        """Queue a ``kind`` turn for ``user`` in the caller's transaction; the item's id.

        Nothing is told: the caller calls :meth:`notify` once it has committed.
        """
        item = QueueItem(
            kind=_STORED_CHAT if kind == "chat" else kind,
            user_name=user,
            source=source,
            text=text,
            telegram_message_id=telegram_message_id,
            reminder_id=reminder_id,
            reminder_due_at=reminder_due_at,
        )
        session.add(item)
        await session.flush()
        return item.id

    def notify(self) -> None:
        """Items were queued and committed: wake the worker, and tell the sweeper."""
        self.activity.notify()

    async def enqueue_ask(self, source: str, prompt: str, user: str) -> int:
        """Queue ``source``'s ``/ask`` prompt as a turn for ``user``."""
        async with self._sessions.begin() as session:
            item_id = await self.add(session, user, "ask", text=prompt, source=source)
        self.notify()
        return item_id

    async def head(self) -> QueueItem | None:
        """The oldest pending item, which must finish before any other starts."""
        async with self._sessions() as session:
            return await session.scalar(
                select(QueueItem)
                .where(QueueItem.status == "pending")
                .order_by(QueueItem.id)
                .limit(1)
            )

    async def retry_later(self, item_id: int, attempts: int, at: datetime, reason: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(QueueItem)
                .where(QueueItem.id == item_id)
                .values(attempts=attempts, next_attempt_at=at, failure_reason=reason)
            )

    async def finish(self, item_id: int, status: str, reason: str | None = None) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(QueueItem)
                .where(QueueItem.id == item_id)
                .values(status=status, failure_reason=reason, finished_at=datetime.now(UTC))
            )


async def busy(session: AsyncSession, user: str) -> bool:
    """Whether ``user`` has an item queued, running or waiting for their answer.

    Read in the caller's transaction: ``History.close_idle``'s ``busy``.
    """
    found = await session.scalar(
        select(QueueItem.id)
        .where(QueueItem.status.in_(("pending", "waiting")), QueueItem.user_name == user)
        .limit(1)
    )
    return found is not None


def turn_kind(item: QueueItem) -> TurnKind:
    """The kind of turn ``item`` asks for."""
    return "chat" if item.kind == _STORED_CHAT else cast(TurnKind, item.kind)
