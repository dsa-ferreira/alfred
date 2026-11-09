"""The AI-bound queue and its single worker (design D8).

Owner messages, ``/ask`` prompts and due reminders are rows in
``queue_items``. One worker
takes the oldest ``pending`` row and runs its turn; nothing behind it starts
until it is done or failed. While the AI is unreachable (and no tool has run
in the turn) the head item is retried with backoff and keeps its place.
Pending rows simply resume after a restart.

A reminder always reaches the owner (change add-reminders, design D6): a
blank or ``NO_REPLY`` answer, or a turn that fails for good, is replaced by
the fixed reminder notice.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.agent.prompts import is_no_reply
from alfred.agent.turn import TurnFailed, TurnResult
from alfred.attachments import NewAttachment
from alfred.backoff import backoff_s
from alfred.config import QueueConfig
from alfred.db.models import Attachment, AttachmentPage, QueueItem
from alfred.history import History
from alfred.reminders.store import ReminderRow
from alfred.tools.base import TurnInput

log = logging.getLogger(__name__)


class Outbox(Protocol):
    """Sends text to the owner. Raises if the message couldn't be delivered."""

    async def send(self, text: str) -> None: ...


class TurnRunner(Protocol):
    async def run(self, turn: TurnInput) -> TurnResult: ...


class ReminderLookup(Protocol):
    async def get(self, reminder_id: int) -> ReminderRow | None: ...


class Summariser(Protocol):
    async def summarise(self, conversation_id: int) -> str | None:
        """The archived conversation's title, or ``None``. Never raises."""
        ...


def reset_marker(archived_id: int | None, title: str | None, new_id: int) -> str:
    """The one message that tells the owner a conversation ended (memory design D3)."""
    lines: list[str] = []
    if archived_id is not None:
        lines.append(f'— #{archived_id} "{title}" —' if title else f"— #{archived_id} archived —")
    lines.append(f"— new conversation #{new_id} —")
    return "\n".join(lines)


class Queue:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self.signal = asyncio.Event()

    async def enqueue_owner(
        self,
        text: str,
        telegram_message_id: int | None = None,
        attachments: Sequence[NewAttachment] = (),
    ) -> int:
        """Queue an owner message, with its attachments, in one transaction.

        The files must already be in the object store: the commit is what
        makes the message exist (change add-attachments, design D2).
        """
        async with self._sessions.begin() as session:
            item = QueueItem(kind="owner", text=text, telegram_message_id=telegram_message_id)
            session.add(item)
            await session.flush()
            for position, new in enumerate(attachments):
                row = Attachment(
                    queue_item_id=item.id,
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
        self.signal.set()
        return item_id

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
        owner_address: str,
        memory: Summariser | None = None,
        typing: Callable[[], AbstractAsyncContextManager[None]] | None = None,
        reminders: ReminderLookup | None = None,
        zone: ZoneInfo | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._queue = queue
        # Shown to the owner while their own turn runs (typing design D1).
        self._typing = typing
        self._memory = memory
        self._agent = agent
        self._history = history
        self._outbox = outbox
        self._config = config
        self._owner_address = owner_address
        self._reminders = reminders
        self._zone = zone or ZoneInfo("UTC")
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
        if item.kind == "reminder":
            turn = await self._reminder_turn(item)
        else:
            turn = TurnInput(item.kind, item.text, item.id, item.source)  # type: ignore[arg-type]
        indicator = (
            self._typing()
            if self._typing is not None and item.kind == "owner"
            else contextlib.nullcontext()
        )
        try:
            async with indicator:
                result = await self._agent.run(turn)
        except TurnFailed as failure:
            await self._turn_failed(item, turn, failure)
            return
        except Exception as exc:
            log.exception("queue item %s: unexpected error", item.id)
            await self._turn_failed(
                item,
                turn,
                TurnFailed(
                    f"internal error: {type(exc).__name__}",
                    notices.failed(self._owner_address),
                    retryable=False,
                    side_effects_possible=True,
                ),
            )
            return
        await self._complete(item, turn, result)

    async def _reminder_turn(self, item: QueueItem) -> TurnInput:
        """The reminder's text and due time, read through ``reminder_id``."""
        reminder = None
        if self._reminders is not None and item.reminder_id is not None:
            reminder = await self._reminders.get(item.reminder_id)
        if reminder is None:
            log.error("queue item %s: reminder #%s not found", item.id, item.reminder_id)
            return TurnInput(
                "reminder", item.text, item.id, reminder_id=item.reminder_id, due_at=item.created_at
            )
        return TurnInput(
            "reminder", reminder.text, item.id, reminder_id=reminder.id, due_at=reminder.due_at
        )

    def _reminder_notice(self, turn: TurnInput) -> str:
        due = (turn.due_at or self._now()).astimezone(self._zone)
        today = self._now().astimezone(self._zone).date()
        return notices.reminder(self._owner_address, turn.text, due, today)

    async def _deliver_reminder(self, item: QueueItem, turn: TurnInput, message: str) -> None:
        """Record the reminder and what the owner was told, then send it."""
        await self._history.append(
            "reminder", turn.text, queue_item_id=item.id, reminder_id=turn.reminder_id
        )
        await self._history.append(
            "reminder_reply", message, queue_item_id=item.id, reminder_id=turn.reminder_id
        )
        await self._send(item, message)

    async def _turn_failed(self, item: QueueItem, turn: TurnInput, failure: TurnFailed) -> None:
        if failure.fallback_notice is not None:
            await self._send(item, failure.fallback_notice)
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
        if item.kind == "reminder":
            # Before finishing, so a crash in between can't lose the reminder.
            await self._deliver_reminder(item, turn, self._reminder_notice(turn))
        await self._queue.finish(item.id, "failed", failure.reason)
        if item.kind == "owner":
            await self._send(item, failure.notice)

    async def _complete(self, item: QueueItem, turn: TurnInput, result: TurnResult) -> None:
        # The owner's brain choice changed, so they hear of it even when an
        # /ask ends in NO_REPLY. Not recorded: the next prompt names the brain.
        if result.fallback_notice is not None:
            await self._send(item, result.fallback_notice)
        if item.kind == "owner":
            await self._history.append("assistant", result.reply, queue_item_id=item.id)
            await self._send(item, result.reply)
        elif item.kind == "reminder":
            reply = result.reply
            if not reply.strip() or is_no_reply(reply):
                log.warning("reminder item %s: the model gave no message; plain notice", item.id)
                reply = self._reminder_notice(turn)
            await self._deliver_reminder(item, turn, reply)
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
            await self._reset(item)

    async def _reset(self, item: QueueItem) -> None:
        # Archive first: anything recorded while the summary is made (a
        # /notify notice) already lands in the new conversation.
        reset = await self._history.reset()
        log.info(
            "queue item %s: conversation %s archived, %s started",
            item.id,
            reset.archived_id,
            reset.new_id,
        )
        title = None
        if self._memory is not None and reset.archived_id is not None:
            title = await self._memory.summarise(reset.archived_id)
        await self._send(item, reset_marker(reset.archived_id, title, reset.new_id))

    async def _send(self, item: QueueItem, text: str) -> None:
        try:
            await self._outbox.send(text)
        except Exception:
            # The turn already ran; re-running it would repeat tool actions.
            log.exception("queue item %s: could not deliver a message to the owner", item.id)
