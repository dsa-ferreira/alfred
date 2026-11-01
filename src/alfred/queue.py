"""The AI-bound queue and its single worker (design D8).

Owner messages and ``/ask`` prompts are rows in ``queue_items``. One worker
takes the oldest ``pending`` row and runs its turn; nothing behind it starts
until it is done or failed. While the AI is unreachable (and no tool has run
in the turn) the head item is retried with backoff and keeps its place.
Pending rows simply resume after a restart.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.prompts import is_no_reply
from alfred.agent.turn import TurnFailed, TurnInput, TurnResult
from alfred.config import QueueConfig
from alfred.db.models import QueueItem
from alfred.history import History

log = logging.getLogger(__name__)

NEW_CONVERSATION_MARKER = "— new conversation —"
GENERIC_FAILURE = "Sorry, that request failed."


class Outbox(Protocol):
    """Sends text to the owner. Raises if the message couldn't be delivered."""

    async def send(self, text: str) -> None: ...


class TurnRunner(Protocol):
    async def run(self, turn: TurnInput) -> TurnResult: ...


def backoff_s(attempts: int, config: QueueConfig) -> float:
    """Delay before retry number ``attempts`` (1-based): doubles up to the maximum."""
    return min(config.retry_initial_s * 2 ** (attempts - 1), config.retry_max_s)


class Queue:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self.signal = asyncio.Event()

    async def enqueue_owner(self, text: str, telegram_message_id: int | None = None) -> int:
        return await self._insert(
            QueueItem(kind="owner", text=text, telegram_message_id=telegram_message_id)
        )

    async def enqueue_ask(self, source: str, prompt: str) -> int:
        return await self._insert(QueueItem(kind="ask", source=source, text=prompt))

    async def _insert(self, item: QueueItem) -> int:
        async with self._sessions.begin() as session:
            session.add(item)
            await session.flush()
            item_id = item.id
        self.signal.set()
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


class Worker:
    def __init__(
        self,
        queue: Queue,
        agent: TurnRunner,
        history: History,
        outbox: Outbox,
        config: QueueConfig,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._queue = queue
        self._agent = agent
        self._history = history
        self._outbox = outbox
        self._config = config
        self._now = now

    async def run_forever(self) -> None:
        log.info("queue worker started")
        while True:
            if not await self.step():
                await self._queue.signal.wait()

    async def step(self) -> bool:
        """Process or wait for the head item. Returns False when the queue is empty."""
        self._queue.signal.clear()  # before reading, so a new item is never missed
        item = await self._queue.head()
        if item is None:
            return False
        delay = (item.next_attempt_at - self._now()).total_seconds()
        if delay > 0:
            # Head-of-line: nothing behind it may start, so new items don't matter.
            await asyncio.sleep(delay)
            return True
        await self._process(item)
        return True

    async def _process(self, item: QueueItem) -> None:
        if item.kind == "owner":
            # Recorded when the turn starts (once, even if retried), so it is
            # the newest entry of its own context and after earlier replies.
            await self._history.append("owner", item.text, queue_item_id=item.id)
        turn = TurnInput(item.kind, item.text, item.id, item.source)  # type: ignore[arg-type]
        try:
            result = await self._agent.run(turn)
        except TurnFailed as failure:
            await self._turn_failed(item, failure)
            return
        except Exception as exc:
            log.exception("queue item %s: unexpected error", item.id)
            await self._turn_failed(
                item,
                TurnFailed(
                    f"internal error: {type(exc).__name__}",
                    GENERIC_FAILURE,
                    retryable=False,
                    side_effects_possible=True,
                ),
            )
            return
        await self._complete(item, result)

    async def _turn_failed(self, item: QueueItem, failure: TurnFailed) -> None:
        if failure.retryable:
            attempts = item.attempts + 1
            delay = backoff_s(attempts, self._config)
            log.warning(
                "queue item %s: %s; retry %d in %gs", item.id, failure.reason, attempts, delay
            )
            await self._queue.retry_later(
                item.id, attempts, self._now() + timedelta(seconds=delay), failure.reason
            )
            return
        log.error("queue item %s (%s) failed: %s", item.id, item.kind, failure.reason)
        await self._queue.finish(item.id, "failed", failure.reason)
        if item.kind == "owner":
            await self._send(item, failure.notice)

    async def _complete(self, item: QueueItem, result: TurnResult) -> None:
        if item.kind == "owner":
            await self._history.append("assistant", result.reply, queue_item_id=item.id)
            await self._send(item, result.reply)
        elif is_no_reply(result.reply):
            log.info("ask %s from %s: NO_REPLY, owner not notified", item.id, item.source)
        else:
            await self._history.append(
                "ask_prompt", item.text, source=item.source, queue_item_id=item.id
            )
            await self._history.append(
                "ask_reply", result.reply, source=item.source, queue_item_id=item.id
            )
            await self._send(item, f"[{item.source}] {result.reply}")
        await self._queue.finish(item.id, "done")

        if result.reset_requested:
            await self._history.reset()
            log.info("queue item %s: conversation reset", item.id)
            await self._send(item, NEW_CONVERSATION_MARKER)

    async def _send(self, item: QueueItem, text: str) -> None:
        try:
            await self._outbox.send(text)
        except Exception:
            # The turn already ran; re-running it would repeat tool actions.
            log.exception("queue item %s: could not deliver a message to the owner", item.id)
