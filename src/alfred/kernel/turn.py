"""What a turn answers: a user's chat message, an ``/ask`` prompt or a due reminder.

A turn is a :class:`ChatTurn`, an :class:`AskTurn` or a :class:`ReminderTurn`
(``TurnInput`` is their union): plain data. Toolsets tell them apart with
``isinstance`` (``alfred.kernel.toolset.chat_only``); everything a kind does
is in ``alfred.turns.kinds``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from alfred.kernel.users import User


@dataclass(frozen=True)
class _Turn:
    """What every turn has. ``user`` is whose turn it is."""

    text: str
    queue_item_id: int
    user: User = field(kw_only=True)
    # Set when a parked turn goes on with its user's answer.
    resume: Resume | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class ChatTurn(_Turn):
    """A user's chat message, already in the history when its turn runs."""


@dataclass(frozen=True)
class AskTurn(_Turn):
    """An ``/ask`` prompt from a service, for ``user``.

    Only recorded if the answer reaches the user, so the agent adds it to
    the messages itself.
    """

    source: str


@dataclass(frozen=True)
class ReminderTurn(_Turn):
    """A due reminder, for one of its recipients (``user``).

    ``due_at`` is when this occurrence was due, ``set_by`` who set it when
    not ``user``, ``repeat_label`` its repeat rule as shown ("every weekday
    at 08:00"), and ``quiet_ok`` whether the turn may answer ``NO_REPLY``.
    """

    reminder_id: int | None = None
    due_at: datetime | None = None
    set_by: str | None = None
    repeat_label: str | None = None
    quiet_ok: bool = False


TurnInput = ChatTurn | AskTurn | ReminderTurn


@dataclass(frozen=True)
class Resume:
    """A parked turn's saved progress, and the answer it goes on with.

    ``turn`` is what the agent saved when it parked (JSON); ``state`` and
    ``answer`` go back to the toolset that asked.
    """

    turn: dict[str, Any]
    state: dict[str, Any]
    answer: Any
