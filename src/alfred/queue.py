"""The AI-bound queue and its single worker (design D8).

Users' messages, ``/ask`` prompts and due reminders are rows in
``queue_items``, each for one user. One worker, for all users,
takes the oldest ``pending`` row and runs its turn; nothing behind it starts
until it is done or failed. While the AI is unreachable (and no tool has run
in the turn) the head item is retried with backoff and keeps its place.
Pending rows simply resume after a restart.

The worker resolves the item's user from the config (change
add-multiple-users, design D2); an item for a user who is no longer
configured is marked failed instead of blocking the queue. Replies and fixed
notices go to that user, in their form of address; a brain fallback is also
told to the admin (design D11).

A user's message starts with the marker of a conversation closed for
inactivity, if one is pending (change close-idle-conversations, design D4).
Ending a conversation only archives it: the memory sweeper summarises it in
the background, so the next item never waits (design D1). The queue tells
the sweeper when items arrive and finish (``QueueActivity``, design D5).

A reminder always reaches its recipient (change add-reminders, design D6): a
blank or ``NO_REPLY`` answer, or a turn that fails for good, is replaced by
the fixed reminder notice.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.agent.prompts import is_no_reply
from alfred.agent.turn import Fallback, TurnFailed, TurnResult
from alfred.attachments import NewAttachment
from alfred.backoff import backoff_s
from alfred.config import QueueConfig, User, Users
from alfred.db.models import Attachment, AttachmentPage, QueueItem
from alfred.history import History
from alfred.reminders.store import ReminderRow
from alfred.tools.base import TurnInput

log = logging.getLogger(__name__)


class Outbox(Protocol):
    """Sends text to a user's chat. Raises if the message couldn't be delivered."""

    async def send(self, user: User, text: str) -> None: ...


class TurnRunner(Protocol):
    async def run(self, turn: TurnInput) -> TurnResult: ...


class ReminderLookup(Protocol):
    async def get(self, reminder_id: int) -> ReminderRow | None: ...


def reset_marker(archived_id: int | None, new_id: int, *, idle: bool = False) -> str:
    """The one message that tells the user a conversation ended.

    Ids only: the title is made later, in the background (change
    close-idle-conversations, design D1). ``idle`` for a conversation closed
    after inactivity.
    """
    lines: list[str] = []
    if archived_id is not None:
        how = "closed after inactivity" if idle else "archived"
        lines.append(f"— #{archived_id} {how} —")
    lines.append(f"— new conversation #{new_id} —")
    return "\n".join(lines)


class QueueActivity:
    """When items arrive and finish, for the memory sweeper (change
    close-idle-conversations, design D5).

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

    async def enqueue_user(
        self,
        user: str,
        text: str,
        telegram_message_id: int | None = None,
        attachments: Sequence[NewAttachment] = (),
    ) -> int:
        """Queue ``user``'s message, with its attachments, in one transaction.

        The files must already be in the object store: the commit is what
        makes the message exist (change add-attachments, design D2). They
        belong to ``user``.
        """
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

    async def _insert(self, item: QueueItem) -> int:
        async with self._sessions.begin() as session:
            session.add(item)
            await session.flush()
            item_id = item.id
        self.activity.notify()
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
        users: Users,
        nudge: Callable[[], None] | None = None,
        typing: Callable[[User], AbstractAsyncContextManager[None]] | None = None,
        reminders: ReminderLookup | None = None,
        zone: ZoneInfo | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._queue = queue
        # Shown in a user's chat while their own chat turn runs (typing design D1).
        self._typing = typing
        # Wakes the memory sweeper after a reset (design D1); None in tests.
        self._nudge = nudge
        self._agent = agent
        self._history = history
        self._outbox = outbox
        self._config = config
        self._users = users
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
        try:
            await self._process(item)
        finally:
            self._queue.activity.finished()
        return True

    async def _process(self, item: QueueItem) -> None:
        user = self._users.get(item.user_name)
        if user is None:
            # Not retried: the config, not the AI, has to change (design D9).
            reason = f"user {item.user_name!r} is not configured"
            log.error("queue item %s (%s): %s; marked failed", item.id, item.kind, reason)
            await self._queue.finish(item.id, "failed", reason)
            return
        if item.kind == "user":
            # A conversation closed for inactivity is told before the reply
            # (change close-idle-conversations, design D4). Claimed before it
            # is sent, so a retried turn doesn't send it again.
            marker = await self._history.take_idle_marker(user.name)
            if marker is not None:
                await self._send(item, user, reset_marker(*marker, idle=True))
            # Recorded when the turn starts (once, even if retried), so it is
            # the newest entry of its own context and after earlier replies.
            await self._history.append(user.name, "user", item.text, queue_item_id=item.id)
        if item.kind == "reminder":
            turn = await self._reminder_turn(item, user)
        else:
            kind = "chat" if item.kind == "user" else item.kind
            turn = TurnInput(kind, item.text, item.id, item.source, user=user)  # type: ignore[arg-type]
        indicator = (
            self._typing(user)
            if self._typing is not None and item.kind == "user"
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
                    notices.failed(user.address),
                    retryable=False,
                    side_effects_possible=True,
                ),
            )
            return
        await self._complete(item, turn, result)

    async def _reminder_turn(self, item: QueueItem, user: User) -> TurnInput:
        """The reminder's text, due time and creator, read through ``reminder_id``."""
        reminder = None
        if self._reminders is not None and item.reminder_id is not None:
            reminder = await self._reminders.get(item.reminder_id)
        if reminder is None:
            log.error("queue item %s: reminder #%s not found", item.id, item.reminder_id)
            return TurnInput(
                "reminder",
                item.text,
                item.id,
                reminder_id=item.reminder_id,
                due_at=item.created_at,
                user=user,
            )
        return TurnInput(
            "reminder",
            reminder.text,
            item.id,
            reminder_id=reminder.id,
            due_at=reminder.due_at,
            set_by=reminder.created_by if reminder.created_by != user.name else None,
            user=user,
        )

    def _reminder_notice(self, turn: TurnInput) -> str:
        due = (turn.due_at or self._now()).astimezone(self._zone)
        today = self._now().astimezone(self._zone).date()
        return notices.reminder(turn.user.address, turn.text, due, today)

    async def _deliver_reminder(self, item: QueueItem, turn: TurnInput, message: str) -> None:
        """Record the reminder and what the recipient was told, then send it."""
        user = turn.user
        await self._history.append(
            user.name, "reminder", turn.text, queue_item_id=item.id, reminder_id=turn.reminder_id
        )
        await self._history.append(
            user.name,
            "reminder_reply",
            message,
            queue_item_id=item.id,
            reminder_id=turn.reminder_id,
        )
        await self._send(item, user, message)

    async def _tell_fallback(self, item: QueueItem, user: User, fallback: Fallback | None) -> None:
        """The brain is alfred-wide: the turn's user and the admin hear of a fallback (D11).

        Not recorded: the next prompt names the brain.
        """
        if fallback is None:
            return
        admin = self._users.admin
        for recipient in (user,) if admin.name == user.name else (user, admin):
            notice = notices.fell_back(recipient.address, fallback.failed, fallback.default)
            await self._send(item, recipient, notice)

    async def _turn_failed(self, item: QueueItem, turn: TurnInput, failure: TurnFailed) -> None:
        await self._tell_fallback(item, turn.user, failure.fallback)
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
        if item.kind == "user":
            await self._send(item, turn.user, failure.notice)

    async def _complete(self, item: QueueItem, turn: TurnInput, result: TurnResult) -> None:
        # The brain choice changed, so it is told even when an /ask ends in NO_REPLY.
        await self._tell_fallback(item, turn.user, result.fallback)
        user = turn.user
        if item.kind == "user":
            await self._history.append(user.name, "assistant", result.reply, queue_item_id=item.id)
            await self._send(item, user, result.reply)
        elif item.kind == "reminder":
            reply = result.reply
            if not reply.strip() or is_no_reply(reply):
                log.warning("reminder item %s: the model gave no message; plain notice", item.id)
                reply = self._reminder_notice(turn)
            await self._deliver_reminder(item, turn, reply)
        elif is_no_reply(result.reply):
            log.info("ask %s from %s: NO_REPLY, %s not notified", item.id, item.source, user.name)
        else:
            await self._history.append(
                user.name, "ask_prompt", item.text, source=item.source, queue_item_id=item.id
            )
            await self._history.append(
                user.name, "ask_reply", result.reply, source=item.source, queue_item_id=item.id
            )
            await self._send(item, user, f"[{item.source}] {result.reply}")
        await self._queue.finish(item.id, "done")

        if result.reset_requested:
            await self._reset(item, user)

    async def _reset(self, item: QueueItem, user: User) -> None:
        # The archived conversation is summarised by the memory sweeper, so the
        # next item never waits for it (change close-idle-conversations, D1).
        reset = await self._history.reset(user.name)
        log.info(
            "queue item %s: %s's conversation %s archived, %s started",
            item.id,
            user.name,
            reset.archived_id,
            reset.new_id,
        )
        if self._nudge is not None and reset.archived_id is not None:
            self._nudge()
        await self._send(item, user, reset_marker(reset.archived_id, reset.new_id))

    async def _send(self, item: QueueItem, user: User, text: str) -> None:
        try:
            await self._outbox.send(user, text)
        except Exception:
            # The turn already ran; re-running it would repeat tool actions.
            log.exception("queue item %s: could not deliver a message to %s", item.id, user.name)
