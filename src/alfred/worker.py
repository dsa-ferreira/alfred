"""The worker: runs the queue's items, one at a time, for all users.

**Order.** The worker takes the oldest ``pending`` item and runs its turn;
nothing behind it starts until it is done or failed. While the AI is
unreachable (and no tool has run in the turn) the head item is retried with
backoff and keeps its place.

**Users.** The worker resolves the item's user from the config; an item for
a user who is no longer configured is marked failed instead of blocking the
queue. Replies and fixed notices go to that user, in their form of address;
a brain fallback is also told to the admin.

**Kinds.** What differs between chat, ``/ask`` and reminder turns (what is
recorded, what is sent, what a failure sends) is in :mod:`alfred.kinds`.
A turn that asks to reset the conversation archives it afterwards; the
memory sweeper summarises it in the background, so the next item never
waits.

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
from alfred.agent.turn import Fallback, TurnFailed, TurnResult
from alfred.backoff import backoff_s
from alfred.config import QueueConfig
from alfred.db.models import QueueItem
from alfred.history import History
from alfred.inputs.parking import Parking
from alfred.inputs.store import RequestRow
from alfred.kernel.outbox import Outbox
from alfred.kernel.turn import TurnInput
from alfred.kernel.users import User, Users
from alfred.kinds import AskKind, ChatKind, Kind, ReminderKind, ReminderLookup
from alfred.queue import Queue, TurnKind, turn_kind

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
        reminders: ReminderLookup,
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
        self._now = now
        self._kinds: dict[TurnKind, Kind] = {
            "chat": ChatKind(history, self._send),
            "ask": AskKind(history, self._send),
            "reminder": ReminderKind(history, self._send, reminders, zone or ZoneInfo("UTC"), now),
        }

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
            log.error("queue item %s (%s): %s; marked failed", item.id, turn_kind(item), reason)
            await self._queue.finish(item.id, "failed", reason)
            return
        kind = self._kinds[turn_kind(item)]
        if await self._parking.stopped_mid_call(item.id, user.name):
            await self._turn_failed(
                item,
                await kind.turn(item, user),
                TurnFailed(
                    "stopped while an answered call was running; not run again",
                    notices.failed(user.address, True),
                    retryable=False,
                    side_effects_possible=True,
                ),
            )
            return
        request = await self._parking.take_answer(item.id, user.name)
        if request is None:
            await kind.start(item, user)
        turn = await kind.turn(item, user)
        if request is not None:
            turn = self._parking.resumed(turn, request)
        indicator = self._typing(user) if kind.shows_typing else contextlib.nullcontext()
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
            delay = backoff_s(attempts, self._config.retry_initial_s, self._config.retry_max_s)
            log.warning(
                "queue item %s: %s; retry %d in %gs", item.id, failure.reason, attempts, delay
            )
            await self._queue.retry_later(
                item.id, attempts, self._now() + timedelta(seconds=delay), failure.reason
            )
            return
        log.error("queue item %s (%s) failed: %s", item.id, turn_kind(item), failure.reason)

        async def finish() -> None:
            await self._queue.finish(item.id, "failed", failure.reason)

        await self._kinds[turn_kind(item)].fail(item, turn, failure, finish)

    async def _complete(self, item: QueueItem, turn: TurnInput, result: TurnResult) -> None:
        # The brain choice changed, so it is told even when an /ask ends in NO_REPLY.
        await self._tell_fallback(item, turn.user, result.fallback)
        user = turn.user
        await self._kinds[turn_kind(item)].deliver(item, turn, result.reply)
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
        await self._send(item, user, notices.reset_marker(reset.archived_id, reset.new_id))

    async def _send(self, item: QueueItem, user: User, text: str) -> None:
        try:
            await self._outbox.send(user, text)
        except Exception:
            # The turn already ran; re-running it would repeat tool actions.
            log.exception("queue item %s: could not deliver a message to %s", item.id, user.name)


class _TurnRunner(Protocol):
    async def run(self, turn: TurnInput) -> TurnResult: ...
