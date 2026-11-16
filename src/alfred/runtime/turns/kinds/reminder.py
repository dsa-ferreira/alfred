"""A due reminder, for one of its recipients.

A reminder always reaches its recipient: a blank or ``NO_REPLY`` answer, or
a turn that fails for good, is replaced by the fixed reminder notice, which
is recorded and sent before the item finishes. The exception is a quiet
reminder (``quiet_ok``) answered ``NO_REPLY``: nothing is sent or recorded.
A quiet reminder whose turn fails still gets the notice, so a broken AI
doesn't look like "nothing to report".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from alfred.features.conversations.tables import EntryOrigin
from alfred.features.reminders import notices as reminders_notices
from alfred.kernel.turn import ReminderTurn
from alfred.runtime.agent.framing import TimeFormat, TurnRules
from alfred.runtime.agent.prompts import is_no_reply
from alfred.runtime.turns.kinds.base import Deliver, Ending, NewEntry, Opening

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from alfred.kernel.users import User
    from alfred.runtime.agent.results import TurnFailed
    from alfred.runtime.turns.queue import Item

log = logging.getLogger(__name__)

# Longer reminder texts are cut in a question's title.
_TITLE_TEXT_CHARS = 60


@dataclass(frozen=True)
class ReminderPayload:
    """A reminder's occurrence, snapshotted when it fired.

    A reminder's text, creator, rule and quietness never change once it is
    set, so the snapshot is what the reminder says.
    """

    reminder_id: int
    # When this occurrence was due (a repeating reminder's row has moved on).
    due_at: datetime
    text: str
    created_by: str
    # The repeat rule as said to the model ("every weekday at 08:00"); None for a one-shot.
    repeat_label: str | None = None
    quiet_ok: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ReminderPayload:
        return cls(
            data["reminder_id"],
            datetime.fromisoformat(data["due_at"]),
            data["text"],
            data["created_by"],
            data["repeat_label"],
            data["quiet_ok"],
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "reminder_id": self.reminder_id,
            "due_at": self.due_at.isoformat(),
            "text": self.text,
            "created_by": self.created_by,
            "repeat_label": self.repeat_label,
            "quiet_ok": self.quiet_ok,
        }


class ReminderKind:
    tag: ClassVar[str] = "reminder"
    payload: ClassVar[type] = ReminderPayload
    turn_type: ClassVar[type] = ReminderTurn
    shows_typing: ClassVar[bool] = False

    def __init__(self, zone: ZoneInfo) -> None:
        # The fixed notice's due time and date are in alfred's time zone.
        self._zone = zone

    def turn(self, item: Item[ReminderPayload], user: User) -> ReminderTurn:
        """The reminder as it was when it fired: the payload's snapshot."""
        payload = item.payload
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

    def opening(self, item: Item[ReminderPayload], turn: ReminderTurn) -> Opening:
        return Opening()

    def framing(self, turn: ReminderTurn) -> _ReminderFraming:
        return _ReminderFraming(turn)

    def question_title(self, turn: ReminderTurn) -> str:
        """``For reminder #12 (call mum):``, the text cut to 60 characters."""
        text = turn.text
        if len(text) > _TITLE_TEXT_CHARS:
            text = text[: _TITLE_TEXT_CHARS - 1] + "…"
        return f"For reminder #{turn.reminder_id} ({text}):"

    def answer(self, turn: ReminderTurn, reply: str, now: datetime) -> str | None:
        if turn.quiet_ok and is_no_reply(reply):
            log.info(
                "reminder item %s (#%s): NO_REPLY, %s not notified",
                turn.queue_item_id,
                turn.reminder_id,
                turn.user.name,
            )
            return None
        if not reply.strip() or is_no_reply(reply):
            log.warning(
                "reminder item %s: the model gave no message; plain notice", turn.queue_item_id
            )
            return self._notice(turn, now)
        return reply

    def on_failure(self, turn: ReminderTurn, failure: TurnFailed, now: datetime) -> Ending:
        return Deliver(self._notice(turn, now))

    def entries(self, turn: ReminderTurn, text: str) -> list[NewEntry]:
        """The reminder, then what its recipient was told, both from its occurrence."""
        assert turn.reminder_id is not None and turn.due_at is not None, turn
        origin = EntryOrigin(turn.reminder_id, turn.due_at, turn.set_by or turn.user.name)
        return [
            NewEntry("reminder", turn.text, origin=origin),
            NewEntry("reminder_reply", text, origin=origin),
        ]

    def message(self, turn: ReminderTurn, text: str) -> str:
        return text

    def _notice(self, turn: ReminderTurn, now: datetime) -> str:
        assert turn.due_at is not None
        due = turn.due_at.astimezone(self._zone)
        today = now.astimezone(self._zone).date()
        return reminders_notices.reminder(turn.user.address, turn.text, due, today)


@dataclass(frozen=True)
class _ReminderFraming:
    """The reminder rules (quiet or not), and the reminder labelled with its times."""

    turn: ReminderTurn
    inline_attachments: bool = False

    @property
    def rules(self) -> TurnRules:
        return TurnRules(reminder=True, reminder_quiet=self.turn.quiet_ok)

    def input_line(self, now: datetime, times: TimeFormat) -> str | None:
        """``[reminder:#12 due Monday 9 November 2026, 17:00; now 17:12] call mum``.

        Labelled with this attempt's time, so a retried turn shows how late
        it really is. ``; every weekday at 08:00`` after the time when it
        repeats, and ``; set by diogo`` before the ``]`` when someone else
        set it.
        """
        turn = self.turn
        due = turn.due_at or now
        rule = f"; {turn.repeat_label}" if turn.repeat_label else ""
        set_by = f"; set by {turn.set_by}" if turn.set_by else ""
        return (
            f"[reminder:#{turn.reminder_id} due {times.when(due)}; "
            f"now {times.now(now, due)}{rule}{set_by}] {turn.text}"
        )
