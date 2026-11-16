"""What the worker does differently for each kind of turn.

Each kind builds its turn from the queue item, does what has to happen
before the turn first runs, says whether the typing indicator shows, and
delivers the reply or the failure.

- **Chat.** The user's message is recorded when its turn starts (once, even
  if retried), so it is the newest entry of its own context; a conversation
  closed for inactivity is told first. The reply is recorded and sent, and a
  failure sends the failure notice. The typing indicator shows while it runs.
- **Ask.** Nothing is recorded until the answer: then the prompt and the
  reply are, and the reply goes to the user as ``[source] reply``. A
  ``NO_REPLY`` answer, or a failure, sends and records nothing.
- **Reminder.** A reminder always reaches its recipient: a blank or
  ``NO_REPLY`` answer, or a turn that fails for good, is replaced by the
  fixed reminder notice. The exception is a quiet reminder (``quiet_ok``)
  answered ``NO_REPLY``: nothing is sent or recorded. A quiet reminder whose
  turn fails still gets the notice, so a broken AI doesn't look like
  "nothing to report".
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from alfred import notices
from alfred.agent.prompts import is_no_reply
from alfred.agent.turn import TurnFailed
from alfred.conversations import notices as conversations_notices
from alfred.conversations.store import History
from alfred.db.models import QueueItem
from alfred.kernel.turn import AskTurn, ChatTurn, ReminderTurn, TurnInput
from alfred.kernel.users import User
from alfred.reminders.store import ReminderRow

log = logging.getLogger(__name__)

# Sends text to the item's user; logs instead of raising when it can't.
_Send = Callable[[QueueItem, User, str], Awaitable[None]]


class Kind(Protocol):
    # Whether the user's chat shows "typing" while the turn runs.
    shows_typing: bool

    async def turn(self, item: QueueItem, user: User) -> TurnInput:
        """The turn the item asks for."""
        ...

    async def start(self, item: QueueItem, user: User) -> None:
        """What happens before the turn first runs (not when it resumes)."""
        ...

    async def deliver(self, item: QueueItem, turn: TurnInput, reply: str) -> None:
        """Record and send the turn's reply."""
        ...

    async def fail(
        self,
        item: QueueItem,
        turn: TurnInput,
        failure: TurnFailed,
        finish: Callable[[], Awaitable[None]],
    ) -> None:
        """Tell the user, if anyone, that the turn failed for good; ``finish`` marks it."""
        ...


class ReminderLookup(Protocol):
    async def get(self, reminder_id: int) -> ReminderRow | None: ...


class ChatKind:
    shows_typing = True

    def __init__(self, history: History, send: _Send) -> None:
        self._history = history
        self._send = send

    async def turn(self, item: QueueItem, user: User) -> TurnInput:
        return ChatTurn(item.text, item.id, user=user)

    async def start(self, item: QueueItem, user: User) -> None:
        # A conversation closed for inactivity is told before the reply. Claimed
        # before it is sent, so a retried turn doesn't send it again.
        marker = await self._history.take_idle_marker(user.name)
        if marker is not None:
            await self._send(item, user, conversations_notices.reset_marker(*marker, idle=True))
        # Recorded when the turn starts (once, even if retried), so it is the
        # newest entry of its own context and after earlier replies.
        await self._history.append(user.name, "user", item.text, queue_item_id=item.id)

    async def deliver(self, item: QueueItem, turn: TurnInput, reply: str) -> None:
        user = turn.user
        await self._history.append(user.name, "assistant", reply, queue_item_id=item.id)
        await self._send(item, user, reply)

    async def fail(
        self,
        item: QueueItem,
        turn: TurnInput,
        failure: TurnFailed,
        finish: Callable[[], Awaitable[None]],
    ) -> None:
        await finish()
        await self._send(item, turn.user, failure.notice)


class AskKind:
    shows_typing = False

    def __init__(self, history: History, send: _Send) -> None:
        self._history = history
        self._send = send

    async def turn(self, item: QueueItem, user: User) -> TurnInput:
        assert item.source is not None
        return AskTurn(item.text, item.id, item.source, user=user)

    async def start(self, item: QueueItem, user: User) -> None:
        pass

    async def deliver(self, item: QueueItem, turn: TurnInput, reply: str) -> None:
        user = turn.user
        if is_no_reply(reply):
            log.info("ask %s from %s: NO_REPLY, %s not notified", item.id, item.source, user.name)
            return
        await self._history.append(
            user.name, "ask_prompt", item.text, source=item.source, queue_item_id=item.id
        )
        await self._history.append(
            user.name, "ask_reply", reply, source=item.source, queue_item_id=item.id
        )
        await self._send(item, user, f"[{item.source}] {reply}")

    async def fail(
        self,
        item: QueueItem,
        turn: TurnInput,
        failure: TurnFailed,
        finish: Callable[[], Awaitable[None]],
    ) -> None:
        await finish()


class ReminderKind:
    shows_typing = False

    def __init__(
        self,
        history: History,
        send: _Send,
        reminders: ReminderLookup,
        zone: ZoneInfo,
        now: Callable[[], datetime],
    ) -> None:
        self._history = history
        self._send = send
        self._reminders = reminders
        self._zone = zone
        self._now = now

    async def turn(self, item: QueueItem, user: User) -> TurnInput:
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

    async def start(self, item: QueueItem, user: User) -> None:
        pass

    async def deliver(self, item: QueueItem, turn: TurnInput, reply: str) -> None:
        reminder = _reminder(turn)
        if reminder.quiet_ok and is_no_reply(reply):
            log.info(
                "reminder item %s (#%s): NO_REPLY, %s not notified",
                item.id,
                reminder.reminder_id,
                reminder.user.name,
            )
            return
        if not reply.strip() or is_no_reply(reply):
            log.warning("reminder item %s: the model gave no message; plain notice", item.id)
            reply = self._notice(reminder)
        await self._record_and_send(item, reminder, reply)

    async def fail(
        self,
        item: QueueItem,
        turn: TurnInput,
        failure: TurnFailed,
        finish: Callable[[], Awaitable[None]],
    ) -> None:
        # Before finishing, so a crash in between can't lose the reminder.
        reminder = _reminder(turn)
        await self._record_and_send(item, reminder, self._notice(reminder))
        await finish()

    def _notice(self, turn: ReminderTurn) -> str:
        due = (turn.due_at or self._now()).astimezone(self._zone)
        today = self._now().astimezone(self._zone).date()
        return notices.reminder(turn.user.address, turn.text, due, today)

    async def _record_and_send(self, item: QueueItem, turn: ReminderTurn, message: str) -> None:
        """Record the reminder and what the recipient was told, then send it."""
        user = turn.user
        for kind, text in (("reminder", turn.text), ("reminder_reply", message)):
            await self._history.append(
                user.name,
                kind,  # type: ignore[arg-type]
                text,
                queue_item_id=item.id,
                reminder_id=turn.reminder_id,
                reminder_due_at=turn.due_at,
            )
        await self._send(item, user, message)


def _reminder(turn: TurnInput) -> ReminderTurn:
    assert isinstance(turn, ReminderTurn), turn
    return turn
