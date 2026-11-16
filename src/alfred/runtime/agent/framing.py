"""How a turn is framed for the model: what its kind adds around the history.

The agent names no kind, as it names no tool. The worker hands it the
turn's :class:`Framing`, which ``alfred.runtime.turns.kinds`` implements for each
kind: which turn rules the system prompt adds, whether the inline window
shows the history's attachments, and the input line added after the history
(an ``/ask`` prompt or a due reminder, which the history doesn't hold yet).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class TurnRules:
    """Which turn rules the system prompt adds."""

    ask: bool = False
    reminder: bool = False
    # A reminder that may be answered ``NO_REPLY``.
    reminder_quiet: bool = False


class TimeFormat(Protocol):
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


class Framing(Protocol):
    @property
    def rules(self) -> TurnRules:
        """The turn rules the system prompt adds."""
        ...

    @property
    def inline_attachments(self) -> bool:
        """Whether the history's newest attachments are shown to the model inline."""
        ...

    def input_line(self, now: datetime, times: TimeFormat) -> str | None:
        """The message added after the history, at this attempt's ``now``; ``None`` for none."""
        ...
