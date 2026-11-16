"""What a turn answers: a user's chat message, an ``/ask`` prompt or a due reminder.

A turn is a :class:`ChatTurn`, an :class:`AskTurn` or a :class:`ReminderTurn`
(``TurnInput`` is their union). Toolsets tell them apart with ``isinstance``
(``alfred.kernel.toolset.chat_only``). Each carries what the agent needs from
its kind: the system prompt's turn rules and the input line it adds after the
history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

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

    def prompt_flags(self) -> _PromptFlags:
        return _PromptFlags()

    def input_line(self, now: datetime, times: _TimeFormat) -> str | None:
        return None


@dataclass(frozen=True)
class AskTurn(_Turn):
    """An ``/ask`` prompt from a service, for ``user``.

    Only recorded if the answer reaches the user, so the agent adds it to
    the messages itself.
    """

    source: str

    def prompt_flags(self) -> _PromptFlags:
        return _PromptFlags(ask=True)

    def input_line(self, now: datetime, times: _TimeFormat) -> str | None:
        """``[Mon 9 Nov 14:05] [ask:disk-check] 93% full``: after the time now."""
        return times.stamp(f"[ask:{self.source}] {self.text}", now)


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

    def prompt_flags(self) -> _PromptFlags:
        return _PromptFlags(reminder=True, reminder_quiet=self.quiet_ok)

    def input_line(self, now: datetime, times: _TimeFormat) -> str | None:
        """``[reminder:#12 due Monday 9 November 2026, 17:00; now 17:12] call mum``.

        Labelled with this attempt's time, so a retried turn shows how late
        it really is. ``; every weekday at 08:00`` after the time when it
        repeats, and ``; set by diogo`` before the ``]`` when someone else
        set it.
        """
        due = self.due_at or now
        rule = f"; {self.repeat_label}" if self.repeat_label else ""
        set_by = f"; set by {self.set_by}" if self.set_by else ""
        return (
            f"[reminder:#{self.reminder_id} due {times.when(due)}; "
            f"now {times.now(now, due)}{rule}{set_by}] {self.text}"
        )


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


class _TimeFormat(Protocol):
    """Times as the turn's user reads them (in their time zone)."""

    def stamp(self, text: str, moment: datetime) -> str:
        """``text`` after the time it arrived, like a history message."""
        ...

    def when(self, moment: datetime) -> str:
        """A due time: ``Monday 9 November 2026, 17:00``."""
        ...

    def now(self, now: datetime, due: datetime) -> str:
        """The time now, against a due time."""
        ...


@dataclass(frozen=True)
class _PromptFlags:
    """Which turn rules the system prompt adds."""

    ask: bool = False
    reminder: bool = False
    reminder_quiet: bool = False
