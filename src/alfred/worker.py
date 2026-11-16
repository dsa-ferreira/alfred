"""The worker: runs the queue's items, one at a time, for all users.

**Order.** The worker takes the oldest ``pending`` item and runs its turn;
nothing behind it starts until it is done or failed. While the AI is
unreachable (and no tool has run in the turn) the head item is retried with
backoff and keeps its place.

**Users.** The worker resolves the item's user from the config; an item for
a user who is no longer configured is marked failed instead of blocking the
queue. Replies and fixed notices go to that user, in their form of address;
a brain fallback is also told to the admin.

**Conversations.** A user's message starts with the marker of a
conversation closed for inactivity, if one is pending. Ending a
conversation only archives it: the memory sweeper summarises it in the
background, so the next item never waits.

**Reminders.** A reminder always reaches its recipient: a blank or
``NO_REPLY`` answer, or a turn that fails for good, is replaced by the fixed
reminder notice. The exception is a quiet reminder (``quiet_ok``) answered
``NO_REPLY``: nothing is sent or recorded. A quiet reminder whose turn fails
still gets the notice, so a broken AI doesn't look like "nothing to report".

**Parked turns.** A turn may park, waiting for its user's answer; parking
and picking it up again are :class:`alfred.inputs.parking.Parking`'s. An
item whose answered call was running when alfred stopped fails with the
partial notice and never runs again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from alfred import notices
from alfred.agent.prompts import is_no_reply
from alfred.agent.turn import Fallback, TurnFailed, TurnResult
from alfred.backoff import backoff_s
from alfred.config import QueueConfig, User, Users
from alfred.db.models import QueueItem
from alfred.history import History
from alfred.inputs.parking import Parking
from alfred.inputs.store import RequestRow
from alfred.outbox import Outbox
from alfred.queue import Queue
from alfred.reminders.store import ReminderRow
from alfred.tools.base import AskTurn, ChatTurn, ReminderTurn, TurnInput

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        queue: Queue,
        agent: _TurnRunner,
        history: History,
        outbox: Outbox,
        config: QueueConfig,
        *,
        users: Users,
        parking: Parking,
        reminders: _ReminderLookup,
        typing: Callable[[User], AbstractAsyncContextManager[None]],
        nudge: Callable[[], None],
        zone: ZoneInfo | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._queue = queue
        # Turns that wait for their user's answer, and their questions.
        self._parking = parking
        # Shown in a user's chat while their own chat turn runs.
        self._typing = typing
        # Wakes the memory sweeper after a reset.
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
            if not await self._step():
                await self._queue.signal.wait()

    async def _step(self) -> bool:
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
            # Not retried: the config, not the AI, has to change.
            reason = f"user {item.user_name!r} is not configured"
            log.error("queue item %s (%s): %s; marked failed", item.id, item.kind, reason)
            await self._queue.finish(item.id, "failed", reason)
            return
        if await self._parking.stopped_mid_call(item.id, user.name):
            await self._turn_failed(
                item,
                await self._turn_input(item, user),
                TurnFailed(
                    "stopped while an answered call was running; not run again",
                    notices.failed(user.address, True),
                    retryable=False,
                    side_effects_possible=True,
                ),
            )
            return
        request = await self._parking.take_answer(item.id, user.name)
        if item.kind == "user" and request is None:
            # A conversation closed for inactivity is told before the reply. Claimed before it
            # is sent, so a retried turn doesn't send it again.
            marker = await self._history.take_idle_marker(user.name)
            if marker is not None:
                await self._send(item, user, _reset_marker(*marker, idle=True))
            # Recorded when the turn starts (once, even if retried), so it is
            # the newest entry of its own context and after earlier replies.
            await self._history.append(user.name, "user", item.text, queue_item_id=item.id)
        turn = await self._turn_input(item, user)
        if request is not None:
            turn = self._parking.resumed(turn, request)
        indicator = self._typing(user) if item.kind == "user" else contextlib.nullcontext()
        try:
            async with indicator:
                result = await self._run(turn, request)
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
                    notices.failed(user.address, request is not None),
                    retryable=False,
                    side_effects_possible=True,
                ),
            )
            return
        if result.waiting is not None:
            await self._tell_fallback(item, user, result.fallback)
            await self._parking.park(item, turn, result.waiting)
            return
        await self._complete(item, turn, result)

    async def _run(self, turn: TurnInput, request: RequestRow | None) -> TurnResult:
        """Run the turn; a resumed turn's request is ``done`` once it ends, however it ends.

        Before anything else happens with the result, so a turn that parks
        again can open a new request.
        """
        try:
            return await self._agent.run(turn)
        finally:
            if request is not None:
                await self._parking.finish(request)

    async def _turn_input(self, item: QueueItem, user: User) -> TurnInput:
        if item.kind == "reminder":
            return await self._reminder_turn(item, user)
        if item.kind == "ask":
            assert item.source is not None
            return AskTurn(item.text, item.id, item.source, user=user)
        return ChatTurn(item.text, item.id, user=user)

    async def _reminder_turn(self, item: QueueItem, user: User) -> ReminderTurn:
        """The reminder's text, rule, quietness and creator, read through ``reminder_id``.

        The due time is the item's own: a repeating reminder's row has
        already moved on to its next occurrence.
        """
        due_at = item.reminder_due_at
        reminder = None
        if item.reminder_id is not None:
            reminder = await self._reminders.get(item.reminder_id)
        if reminder is None:
            log.error("queue item %s: reminder #%s not found", item.id, item.reminder_id)
            return ReminderTurn(
                item.text,
                item.id,
                reminder_id=item.reminder_id,
                due_at=due_at or item.created_at,
                user=user,
            )
        return ReminderTurn(
            reminder.text,
            item.id,
            reminder_id=reminder.id,
            # Items queued before migration 0008 were backfilled; the row's
            # time is only a last resort.
            due_at=due_at or reminder.due_at,
            set_by=reminder.created_by if reminder.created_by != user.name else None,
            repeat_label=reminder.rule.label() if reminder.rule else None,
            quiet_ok=reminder.quiet_ok,
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
            user.name,
            "reminder",
            turn.text,
            queue_item_id=item.id,
            reminder_id=turn.reminder_id,
            reminder_due_at=turn.due_at,
        )
        await self._history.append(
            user.name,
            "reminder_reply",
            message,
            queue_item_id=item.id,
            reminder_id=turn.reminder_id,
            reminder_due_at=turn.due_at,
        )
        await self._send(item, user, message)

    async def _tell_fallback(self, item: QueueItem, user: User, fallback: Fallback | None) -> None:
        """The brain is alfred-wide: the turn's user and the admin hear of a fallback.

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
            if turn.quiet_ok and is_no_reply(reply):
                log.info(
                    "reminder item %s (#%s): NO_REPLY, %s not notified",
                    item.id,
                    turn.reminder_id,
                    user.name,
                )
            else:
                if not reply.strip() or is_no_reply(reply):
                    log.warning(
                        "reminder item %s: the model gave no message; plain notice", item.id
                    )
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
        # next item never waits for it.
        reset = await self._history.reset(user.name)
        log.info(
            "queue item %s: %s's conversation %s archived, %s started",
            item.id,
            user.name,
            reset.archived_id,
            reset.new_id,
        )
        if reset.archived_id is not None:
            self._nudge()
        await self._send(item, user, _reset_marker(reset.archived_id, reset.new_id))

    async def _send(self, item: QueueItem, user: User, text: str) -> None:
        try:
            await self._outbox.send(user, text)
        except Exception:
            # The turn already ran; re-running it would repeat tool actions.
            log.exception("queue item %s: could not deliver a message to %s", item.id, user.name)


class _TurnRunner(Protocol):
    async def run(self, turn: TurnInput) -> TurnResult: ...


class _ReminderLookup(Protocol):
    async def get(self, reminder_id: int) -> ReminderRow | None: ...


def _reset_marker(archived_id: int | None, new_id: int, *, idle: bool = False) -> str:
    """The one message that tells the user a conversation ended.

    Ids only: the title is made later, in the background. ``idle`` for a conversation closed
    after inactivity.
    """
    lines: list[str] = []
    if archived_id is not None:
        how = "closed after inactivity" if idle else "archived"
        lines.append(f"— #{archived_id} {how} —")
    lines.append(f"— new conversation #{new_id} —")
    return "\n".join(lines)
