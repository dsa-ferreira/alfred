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

from alfred.agent.prompts import is_no_reply
from alfred.agent.results import TurnFailed
from alfred.conversations import notices as conversations_notices
from alfred.conversations.store import History
from alfred.conversations.tables import EntryOrigin
from alfred.kernel.turn import AskTurn, ChatTurn, ReminderTurn, TurnInput
from alfred.kernel.users import User
from alfred.reminders import notices as reminders_notices
from alfred.turns.payloads import AskPayload, ChatPayload, ReminderPayload
from alfred.turns.queue import Item

log = logging.getLogger(__name__)

# Sends text to the item's user; logs instead of raising when it can't.
_Send = Callable[[Item, User, str], Awaitable[None]]


class Kind(Protocol):
    # Whether the user's chat shows "typing" while the turn runs.
    shows_typing: bool

    async def turn(self, item: Item, user: User) -> TurnInput:
        """The turn the item asks for."""
        ...

    async def start(self, item: Item, user: User) -> None:
        """What happens before the turn first runs (not when it resumes)."""
        ...

    async def deliver(self, item: Item, turn: TurnInput, reply: str) -> None:
        """Record and send the turn's reply."""
        ...

    async def fail(
        self,
        item: Item,
        turn: TurnInput,
        failure: TurnFailed,
        finish: Callable[[], Awaitable[None]],
    ) -> None:
        """Tell the user, if anyone, that the turn failed for good; ``finish`` marks it."""
        ...


class ChatKind:
    shows_typing = True

    def __init__(self, history: History, send: _Send) -> None:
        self._history = history
        self._send = send

    async def turn(self, item: Item, user: User) -> TurnInput:
        return ChatTurn(_chat(item).text, item.id, user=user)

    async def start(self, item: Item, user: User) -> None:
        # A conversation closed for inactivity is told before the reply. Claimed
        # before it is sent, so a retried turn doesn't send it again.
        marker = await self._history.take_idle_marker(user.name)
        if marker is not None:
            await self._send(item, user, conversations_notices.reset_marker(*marker, idle=True))
        # Recorded when the turn starts (once, even if retried), so it is the
        # newest entry of its own context and after earlier replies.
        payload = _chat(item)
        await self._history.append(
            user.name,
            "user",
            payload.text,
            turn_id=item.id,
            attachment_ids=payload.attachment_ids,
        )

    async def deliver(self, item: Item, turn: TurnInput, reply: str) -> None:
        user = turn.user
        await self._history.append(user.name, "assistant", reply, turn_id=item.id)
        await self._send(item, user, reply)

    async def fail(
        self,
        item: Item,
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

    async def turn(self, item: Item, user: User) -> TurnInput:
        payload = _ask(item)
        return AskTurn(payload.prompt, item.id, payload.source, user=user)

    async def start(self, item: Item, user: User) -> None:
        pass

    async def deliver(self, item: Item, turn: TurnInput, reply: str) -> None:
        user = turn.user
        source = _ask(item).source
        if is_no_reply(reply):
            log.info("ask %s from %s: NO_REPLY, %s not notified", item.id, source, user.name)
            return
        await self._history.append(
            user.name, "ask_prompt", _ask(item).prompt, source=source, turn_id=item.id
        )
        await self._history.append(user.name, "ask_reply", reply, source=source, turn_id=item.id)
        await self._send(item, user, f"[{source}] {reply}")

    async def fail(
        self,
        item: Item,
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
        zone: ZoneInfo,
        now: Callable[[], datetime],
    ) -> None:
        self._history = history
        self._send = send
        self._zone = zone
        self._now = now

    async def turn(self, item: Item, user: User) -> TurnInput:
        """The reminder as it was when it fired: the payload's snapshot."""
        payload = _reminder_payload(item)
        return ReminderTurn(
            payload.text,
            item.id,
            reminder_id=payload.reminder_id,
            due_at=payload.due_at,
            set_by=payload.created_by if payload.created_by != user.name else None,
            repeat_label=payload.repeat_label,
            quiet_ok=payload.quiet_ok,
            user=user,
        )

    async def start(self, item: Item, user: User) -> None:
        pass

    async def deliver(self, item: Item, turn: TurnInput, reply: str) -> None:
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
        item: Item,
        turn: TurnInput,
        failure: TurnFailed,
        finish: Callable[[], Awaitable[None]],
    ) -> None:
        # Before finishing, so a crash in between can't lose the reminder.
        reminder = _reminder(turn)
        await self._record_and_send(item, reminder, self._notice(reminder))
        await finish()

    def _notice(self, turn: ReminderTurn) -> str:
        assert turn.due_at is not None
        due = turn.due_at.astimezone(self._zone)
        today = self._now().astimezone(self._zone).date()
        return reminders_notices.reminder(turn.user.address, turn.text, due, today)

    async def _record_and_send(self, item: Item, turn: ReminderTurn, message: str) -> None:
        """Record the reminder and what the recipient was told, then send it."""
        user = turn.user
        payload = _reminder_payload(item)
        origin = EntryOrigin(payload.reminder_id, payload.due_at, payload.created_by)
        for kind, text in (("reminder", turn.text), ("reminder_reply", message)):
            await self._history.append(
                user.name,
                kind,  # type: ignore[arg-type]
                text,
                turn_id=item.id,
                origin=origin,
            )
        await self._send(item, user, message)


def _reminder(turn: TurnInput) -> ReminderTurn:
    assert isinstance(turn, ReminderTurn), turn
    return turn


def _chat(item: Item) -> ChatPayload:
    assert isinstance(item.payload, ChatPayload), item.payload
    return item.payload


def _ask(item: Item) -> AskPayload:
    assert isinstance(item.payload, AskPayload), item.payload
    return item.payload


def _reminder_payload(item: Item) -> ReminderPayload:
    assert isinstance(item.payload, ReminderPayload), item.payload
    return item.payload
