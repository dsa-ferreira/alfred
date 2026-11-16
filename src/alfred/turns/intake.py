"""Queuing a user's chat message with its files, in one transaction.

```
cancel the user's parked requests (before_message)
begin:  queue item + its attachments and PDF pages
notify the worker
```

A message never exists without its files: if any insert fails, nothing
is queued. This is ``attachments.intake``'s ``Enqueue``, and the bot's for
plain text messages.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.attachments import NewAttachment, rows
from alfred.turns.queue import Queue


class ChatIntake:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        queue: Queue,
        *,
        before_message: Callable[[str], Awaitable[object]] | None = None,
    ) -> None:
        self._sessions = sessions
        self._queue = queue
        # Run with the user's name before their message is queued: cancels
        # their waiting turns.
        self._before_message = before_message

    async def enqueue_user(
        self,
        user: str,
        text: str,
        telegram_message_id: int | None = None,
        attachments: Sequence[NewAttachment] = (),
    ) -> int:
        """Queue ``user``'s message, with its attachments, in one transaction; the item's id.

        The files must already be in the object store: the commit is what
        makes the message exist. They belong to ``user``.

        The user's waiting turns are cancelled first, so the message's turn
        never runs while one of them could still be answered.
        """
        if self._before_message is not None:
            await self._before_message(user)
        async with self._sessions.begin() as session:
            item_id = await self._queue.add(
                session, user, "chat", text=text, telegram_message_id=telegram_message_id
            )
            await rows.add(session, user, attachments, queue_item_id=item_id)
        self._queue.notify()
        return item_id
