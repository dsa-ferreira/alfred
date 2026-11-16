"""The AI-bound queue: users' messages, ``/ask`` prompts and due reminders.

Each is a row in ``queue_items``, for one user. The worker
(``alfred.worker``) takes the oldest ``pending`` row and runs its turn;
nothing behind it starts until it is done or failed. Pending rows simply
resume after a restart. A parked turn's item is ``waiting``, which ``head``
skips; once answered it is ``pending`` again and, being older than anything
queued since, it is the next head.

The queue tells the worker and the memory sweeper when items arrive and
finish (``QueueActivity``). A user's new message first cancels their open
requests (``before_user_message``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.attachments import NewAttachment
from alfred.db.models import Attachment, AttachmentPage, QueueItem

log = logging.getLogger(__name__)


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
        # Run with the user's name before their message is queued: cancels
        # their waiting turns.
        self.before_user_message: Callable[[str], Awaitable[object]] | None = None

    async def has_pending(self) -> bool:
        """Whether an item is waiting or running (it stays pending until it ends)."""
        async with self._sessions() as session:
            found = await session.scalar(
                select(QueueItem.id).where(QueueItem.status == "pending").limit(1)
            )
        return found is not None

    async def enqueue_user(
        self,
        user: str,
        text: str,
        telegram_message_id: int | None = None,
        attachments: Sequence[NewAttachment] = (),
    ) -> int:
        """Queue ``user``'s message, with its attachments, in one transaction.

        The files must already be in the object store: the commit is what
        makes the message exist. They belong to ``user``.

        The user's waiting turns are cancelled first, so the message's turn
        never runs while one of them could still be answered.
        """
        if self.before_user_message is not None:
            await self.before_user_message(user)
        async with self._sessions.begin() as session:
            item = QueueItem(
                kind="user", user_name=user, text=text, telegram_message_id=telegram_message_id
            )
            session.add(item)
            await session.flush()
            for position, new in enumerate(attachments):
                row = Attachment(
                    queue_item_id=item.id,
                    user_name=user,
                    position=position,
                    kind=new.kind,
                    mime=new.mime,
                    file_name=new.file_name,
                    size_bytes=new.size_bytes,
                    object_key=new.object_key,
                    width=new.width,
                    height=new.height,
                    page_count=new.page_count,
                )
                session.add(row)
                await session.flush()
                session.add_all(
                    AttachmentPage(attachment_id=row.id, page=n, text=page)
                    for n, page in enumerate(new.pages, start=1)
                )
            item_id = item.id
        self.activity.notify()
        return item_id

    async def enqueue_ask(self, source: str, prompt: str, user: str) -> int:
        """Queue ``source``'s ``/ask`` prompt as a turn for ``user``."""
        return await self._insert(QueueItem(kind="ask", user_name=user, source=source, text=prompt))

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

    async def _insert(self, item: QueueItem) -> int:
        async with self._sessions.begin() as session:
            session.add(item)
            await session.flush()
            item_id = item.id
        self.activity.notify()
        return item_id
