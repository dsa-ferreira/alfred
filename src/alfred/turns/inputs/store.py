r"""Input requests: a parked turn's question, its answer and its ending.

```
                 tap (owner, not expired)        worker picks item
  open ----------------------------------> answered ----------------> running --> done
   |  \                                     (item: waiting -> pending)   (turn ends)
   |   \-- user's new message --> cancelled
   \------ expiry loop / late tap --> expired       (item -> cancelled, both)
```

Every transition is one transaction that changes a row only from the status
it expects, so a double tap, a tap racing the expiry, or a restart applies
nothing twice. Every query names the user the request belongs to.

A request that ends without an answer (cancelled, expired, or a question that
couldn't be sent) cancels its queue item in the same transaction. The turn's
fixed entries are recorded after it, by item kind, written by alfred: the
model sees in later turns what was not done.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.conversations.store import History
from alfred.kernel.questions import Question
from alfred.kernel.users import Users
from alfred.turns.inputs import notices
from alfred.turns.queue import turn_kind
from alfred.turns.tables import InputRequest, QueueItem

log = logging.getLogger(__name__)

# A request in these statuses keeps its turn parked.
_LIVE_STATUSES = ("open", "answered", "running")
_Answered = Literal["answered", "not_open", "not_yours"]
# Why a request ended unanswered (``alfred.turns.inputs.notices``).
EndReason = Literal["new_message", "expired", "not_delivered"]


@dataclass(frozen=True)
class RequestRow:
    id: int
    queue_item_id: int
    user_name: str
    status: str
    tool: str
    arguments: dict[str, Any]
    question: Question
    state: dict[str, Any]
    answer: Any
    turn: dict[str, Any]
    telegram_message_id: int | None
    cancel_reason: str | None

    @classmethod
    def of(cls, row: InputRequest) -> RequestRow:
        return cls(
            row.id,
            row.queue_item_id,
            row.user_name,
            row.status,
            row.tool,
            row.arguments,
            Question.from_json(row.question),
            row.state,
            row.answer,
            row.turn,
            row.telegram_message_id,
            row.cancel_reason,
        )


class InputRequests:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], history: History, users: Users
    ) -> None:
        self._sessions = sessions
        self._history = history
        self._users = users

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
    ) -> int:
        """Make the item ``waiting`` and store its request, in one transaction.

        ``expires_at`` as a ``timedelta`` is counted from now on the database's
        clock, the one expiry is checked on.
        """
        async with self._sessions.begin() as session:
            await session.execute(
                update(QueueItem)
                .where(QueueItem.id == item_id, QueueItem.user_name == user)
                .values(status="waiting")
            )
            row = InputRequest(
                queue_item_id=item_id,
                user_name=user,
                tool=tool,
                arguments=arguments,
                question=question.to_json(),
                state=state,
                turn=turn,
                expires_at=func.now() + expires_at
                if isinstance(expires_at, timedelta)
                else expires_at,
            )
            session.add(row)
            await session.flush()
            return row.id

    async def set_message(self, request_id: int, user: str, message_id: int) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(InputRequest)
                .where(InputRequest.id == request_id, InputRequest.user_name == user)
                .values(telegram_message_id=message_id)
            )

    async def answer(self, request_id: int, option: int, user: str) -> _Answer:
        """Apply ``user``'s tap on option ``option``: ``open -> answered``, item ``-> pending``.

        Only the request's own user, only while it is open and not expired.
        The row is locked first, so of two taps at once the second sees it
        answered. A tap past the expiry ends the request there and then.
        """
        async with self._sessions.begin() as session:
            found = (
                await session.execute(
                    select(InputRequest, InputRequest.expires_at <= func.now())
                    .where(InputRequest.id == request_id)
                    .with_for_update()
                )
            ).one_or_none()
            if found is None:
                return _Answer("not_open")
            row, past = found
            if row.user_name != user:
                return _Answer("not_yours")
            if row.status != "open":
                return _Answer("not_open", RequestRow.of(row))
            if past:
                ended = await self._end_in(session, _by_id(request_id, user), "expired")
                expired = ended[0] if ended else None
            else:
                options = row.question.get("options", [])
                if not 0 <= option < len(options):
                    return _Answer("not_open", RequestRow.of(row))
                row.status = "answered"
                row.answer = options[option]["answer"]
                row.decided_at = func.now()
                await session.execute(
                    update(QueueItem)
                    .where(
                        QueueItem.id == row.queue_item_id,
                        QueueItem.user_name == user,
                        QueueItem.status == "waiting",
                    )
                    .values(status="pending", next_attempt_at=func.now())
                )
                await session.flush()
                return _Answer("answered", RequestRow.of(row), options[option]["label"])
        await self._record_endings([expired] if expired else [])
        return _Answer("not_open", expired, expired=expired is not None)

    async def take_answered(self, item_id: int, user: str) -> RequestRow | None:
        """``answered -> running`` for the item's request: marked before the call runs again."""
        async with self._sessions.begin() as session:
            row = await session.scalar(
                update(InputRequest)
                .where(
                    InputRequest.queue_item_id == item_id,
                    InputRequest.user_name == user,
                    InputRequest.status == "answered",
                )
                .values(status="running")
                .returning(InputRequest)
            )
            return RequestRow.of(row) if row is not None else None

    async def parked_items(self, user: str) -> set[int]:
        """The queue items of ``user``'s turns that are parked: their request is live."""
        async with self._sessions() as session:
            rows = await session.scalars(
                select(InputRequest.queue_item_id).where(
                    InputRequest.user_name == user, InputRequest.status.in_(_LIVE_STATUSES)
                )
            )
            return set(rows)

    async def running_for(self, item_id: int, user: str) -> RequestRow | None:
        """The item's request left ``running`` (alfred stopped mid-resume), if any."""
        async with self._sessions() as session:
            row = await session.scalar(
                select(InputRequest).where(
                    InputRequest.queue_item_id == item_id,
                    InputRequest.user_name == user,
                    InputRequest.status == "running",
                )
            )
            return RequestRow.of(row) if row is not None else None

    async def finish(self, request_id: int, user: str) -> None:
        """``running -> done``: the resumed turn ended, whatever the outcome."""
        async with self._sessions.begin() as session:
            await session.execute(
                update(InputRequest)
                .where(
                    InputRequest.id == request_id,
                    InputRequest.user_name == user,
                    InputRequest.status == "running",
                )
                .values(status="done")
            )

    async def cancel_for(self, user: str, reason: EndReason = "new_message") -> list[RequestRow]:
        """End all of ``user``'s open requests (``answered`` ones go ahead)."""
        where = (InputRequest.user_name == user, InputRequest.status == "open")
        return await self._end(where, reason)

    async def cancel(self, request_id: int, user: str, reason: EndReason) -> list[RequestRow]:
        """End one open request, such as one whose question couldn't be sent."""
        return await self._end(_by_id(request_id, user), reason)

    async def expire_due(self) -> list[RequestRow]:
        """End every open request past its expiry (the database's clock)."""
        where = (InputRequest.status == "open", InputRequest.expires_at <= func.now())
        return await self._end(where, "expired")

    async def _open_for(self, user: str) -> list[RequestRow]:
        async with self._sessions() as session:
            rows = await session.scalars(
                _ordered(
                    select(InputRequest).where(
                        InputRequest.user_name == user, InputRequest.status == "open"
                    )
                )
            )
            return [RequestRow.of(r) for r in rows]

    async def _end(self, where: Sequence[Any], reason: EndReason) -> list[RequestRow]:
        async with self._sessions.begin() as session:
            ended = await self._end_in(session, where, reason)
        await self._record_endings(ended)
        return ended

    async def _end_in(
        self, session: AsyncSession, where: Sequence[Any], reason: EndReason
    ) -> list[RequestRow]:
        """``open -> cancelled`` (``expired`` for expiry) and the items ``-> cancelled``."""
        status = "expired" if reason == "expired" else "cancelled"
        rows = list(
            await session.scalars(
                update(InputRequest)
                .where(*where)
                .values(status=status, cancel_reason=reason, decided_at=func.now())
                .returning(InputRequest)
            )
        )
        for row in rows:
            await session.execute(
                update(QueueItem)
                .where(
                    QueueItem.id == row.queue_item_id,
                    QueueItem.user_name == row.user_name,
                    QueueItem.status == "waiting",
                )
                .values(status="cancelled", finished_at=func.now(), failure_reason=reason)
            )
        return sorted((RequestRow.of(r) for r in rows), key=lambda r: r.id)

    async def _record_endings(self, ended: Sequence[RequestRow]) -> None:
        """The fixed entries of each turn that ended unanswered, by its item's kind."""
        for request in ended:
            try:
                await self._record_ending(request)
            except Exception:
                log.exception("input request %s: could not record its ending", request.id)

    async def _record_ending(self, request: RequestRow) -> None:
        async with self._sessions() as session:
            item = await session.get(QueueItem, request.queue_item_id)
        if item is None:
            return
        user = self._users.get(request.user_name)
        address = user.address if user is not None else "sir"
        text = notices.input_not_done(address, request.tool, request.cancel_reason or "")
        name, history = request.user_name, self._history
        log.info(
            "input request %s (item %s, %s): ended unanswered (%s)",
            request.id,
            item.id,
            name,
            request.cancel_reason,
        )
        match turn_kind(item):
            case "chat":
                await history.append(name, "assistant", text, queue_item_id=item.id)
            case "ask":
                await history.append(
                    name, "ask_prompt", item.text, source=item.source, queue_item_id=item.id
                )
                await history.append(
                    name, "ask_reply", text, source=item.source, queue_item_id=item.id
                )
            case "reminder":
                for kind, entry_text in (("reminder", item.text), ("reminder_reply", text)):
                    await history.append(
                        name,
                        kind,  # type: ignore[arg-type]
                        entry_text,
                        queue_item_id=item.id,
                        reminder_id=item.reminder_id,
                        reminder_due_at=item.reminder_due_at,
                    )


@dataclass(frozen=True)
class _Answer:
    """What a tap did, and the request it was for (``None`` when there is none)."""

    outcome: _Answered
    request: RequestRow | None = None
    # The label of the option chosen, when answered.
    label: str | None = None
    # Set when the tap found the request past its expiry and ended it.
    expired: bool = False


def _by_id(request_id: int, user: str) -> tuple[Any, ...]:
    return (
        InputRequest.id == request_id,
        InputRequest.user_name == user,
        InputRequest.status == "open",
    )


def _ordered(query: Select[tuple[InputRequest]]) -> Select[tuple[InputRequest]]:
    return query.order_by(InputRequest.id)
