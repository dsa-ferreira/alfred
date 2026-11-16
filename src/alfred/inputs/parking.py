"""Parking a turn that waits for its user's answer, and picking it up again.

The worker hands a turn here at three points:

- before running an item: did alfred stop while the item's answered call was
  running? Then the call may have run, so the request is finished and the
  worker fails the item instead of running it again
  (:meth:`Parking.stopped_mid_call`);
- before running it: is there an answer? Then the request becomes
  ``running`` before the turn resumes, so a crash never runs the call twice,
  and ``done`` once the turn ends (:meth:`Parking.take_answer`,
  :meth:`Parking.resumed`, :meth:`Parking.finish`);
- after running it: did the turn stop at a call that needs the answer? Then
  its request is stored, the item becomes ``waiting``, and the question goes
  to the user; a question that can't be sent ends the request at once
  (:meth:`Parking.park`).
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta
from typing import Any, Protocol

from alfred.config import User
from alfred.db.models import QueueItem
from alfred.inputs.render import reminder_label, title
from alfred.inputs.store import EndReason, RequestRow
from alfred.model import ToolCall
from alfred.tools.base import (
    AskTurn,
    InputNeed,
    Question,
    ReminderTurn,
    Resume,
    TurnInput,
    parse_arguments,
)

log = logging.getLogger(__name__)


class Parking:
    def __init__(
        self,
        requests: _Requests,
        questions: _QuestionOutbox,
        *,
        expire_after: timedelta = timedelta(minutes=60),
    ) -> None:
        self._requests = requests
        self._questions = questions
        self._expire_after = expire_after

    async def stopped_mid_call(self, item_id: int, user: str) -> bool:
        """Whether alfred stopped while this item's answered call was running.

        If so, the request is finished: the call may have run, and the item
        must never run again.
        """
        stuck = await self._requests.running_for(item_id, user)
        if stuck is None:
            return False
        log.error(
            "queue item %s: alfred stopped while %s's answered call %s was running; not run again",
            item_id,
            user,
            stuck.tool,
        )
        await self._requests.finish(stuck.id, user)
        return True

    async def take_answer(self, item_id: int, user: str) -> RequestRow | None:
        """The item's answered request, now ``running``; ``None`` if it has none."""
        return await self._requests.take_answered(item_id, user)

    def resumed(self, turn: TurnInput, request: RequestRow) -> TurnInput:
        """``turn`` going on from where it parked, with the user's answer."""
        log.info("queue item %s: resuming with %s's answer", turn.queue_item_id, turn.user.name)
        return dataclasses.replace(turn, resume=Resume(request.turn, request.state, request.answer))

    async def finish(self, request: RequestRow) -> None:
        """The resumed turn ended, however it ended: its request is ``done``."""
        await self._requests.finish(request.id, request.user_name)

    async def park(self, item: QueueItem, turn: TurnInput, waiting: _Parked) -> None:
        """The turn waits for its user: store the request, then send the question."""
        user = turn.user
        call = waiting.checkpoint.call
        question = dataclasses.replace(waiting.need.question, title=_title(turn))
        arguments = parse_arguments(call.arguments)
        request_id = await self._requests.park(
            item.id,
            user.name,
            tool=call.name,
            arguments=arguments if isinstance(arguments, dict) else {"_raw": call.arguments},
            question=question,
            state=waiting.need.state,
            turn=waiting.checkpoint.to_json(),
            expires_at=self._expire_after,
        )
        try:
            message_id = await self._questions.send_question(user, request_id, question)
        except Exception:
            # The user can't answer what they never saw.
            log.exception("queue item %s: could not send question %s", item.id, request_id)
            await self._requests.cancel(request_id, user.name, "not_delivered")
            return
        await self._requests.set_message(request_id, user.name, message_id)
        log.info(
            "queue item %s: waiting for %s's answer to request %s (%s)",
            item.id,
            user.name,
            request_id,
            call.name,
        )


class _Requests(Protocol):
    """Parked turns' input requests (:class:`alfred.inputs.store.InputRequests`)."""

    async def running_for(self, item_id: int, user: str) -> RequestRow | None: ...

    async def take_answered(self, item_id: int, user: str) -> RequestRow | None: ...

    async def finish(self, request_id: int, user: str) -> None: ...

    async def park(
        self,
        item_id: int,
        user: str,
        *,
        tool: str,
        arguments: dict[str, Any],
        question: Question,
        state: dict[str, Any],
        turn: dict[str, Any],
        expires_at: datetime | timedelta,
    ) -> int: ...

    async def set_message(self, request_id: int, user: str, message_id: int) -> None: ...

    async def cancel(self, request_id: int, user: str, reason: EndReason) -> object: ...


class _QuestionOutbox(Protocol):
    """Sends a question with its buttons, returning its message ID; raises if not delivered."""

    async def send_question(self, user: User, request_id: int, question: Question) -> int: ...


class _Checkpoint(Protocol):
    """Where the turn stopped (the agent's checkpoint)."""

    @property
    def call(self) -> ToolCall: ...

    def to_json(self) -> dict[str, Any]: ...


class _Parked(Protocol):
    """A turn stopped at a call that needs its user's answer (``TurnResult.waiting``)."""

    @property
    def need(self) -> InputNeed: ...

    @property
    def checkpoint(self) -> _Checkpoint: ...


def _title(turn: TurnInput) -> str:
    if isinstance(turn, ReminderTurn):
        return title("reminder", reminder=reminder_label(turn.reminder_id, turn.text))
    if isinstance(turn, AskTurn):
        return title("ask", source=turn.source)
    return title("chat")
