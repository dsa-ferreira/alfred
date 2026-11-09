"""``alfred_remind_set``, ``alfred_reminders_list`` and ``alfred_reminder_cancel``.

Offered in owner turns only (change add-reminders, design D1). alfred
resolves the time itself (``when``), so the result tells the model, and
through it the owner, the exact time and how far away it is. Every failure
becomes an error result for the model.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.toolset import (
    AttemptContext,
    LocalToolset,
    ToolOutcome,
    owner_only,
    parse_arguments,
)
from alfred.config import Config
from alfred.reminders.store import Reminders
from alfred.reminders.when import (
    WhenError,
    format_day_time,
    format_in,
    format_when,
    parse_text,
    resolve,
)

if TYPE_CHECKING:
    from alfred.agent.turn import TurnInput

log = logging.getLogger(__name__)

REMIND_SET = "alfred_remind_set"
REMINDERS_LIST = "alfred_reminders_list"
REMINDER_CANCEL = "alfred_reminder_cancel"
NAMES = frozenset({REMIND_SET, REMINDERS_LIST, REMINDER_CANCEL})

REMIND_SET_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": REMIND_SET,
        "description": (
            "Set a reminder: alfred messages the owner about it at that time. Use for "
            "requests with a time or date ('remind me to call mum at five', 'in 20 minutes', "
            "'on Friday'). If the owner says 'task' or 'todo', or gives no time, use the "
            "to-do list tools instead. Pass the time as the owner said it, as exactly one of "
            "in_minutes or at; never work out times yourself. Confirm to the owner the time "
            "this tool returns."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "What to remind the owner of, e.g. 'call mum'.",
                },
                "in_minutes": {
                    "type": "integer",
                    "description": "Minutes from now, for 'in 20 minutes' or 'in 2 hours' (120).",
                },
                "at": {
                    "type": "string",
                    "description": (
                        "A local time: 'HH:MM' (24-hour; the next time it comes round), "
                        "'YYYY-MM-DD HH:MM', or 'YYYY-MM-DD' (a date with no time)."
                    ),
                },
            },
            "required": ["text"],
        },
    },
}

REMINDERS_LIST_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": REMINDERS_LIST,
        "description": (
            "List the reminders that are set and not yet delivered, soonest first, with their "
            "numbers. Not the to-do list."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

REMINDER_CANCEL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": REMINDER_CANCEL,
        "description": (
            "Cancel a reminder that is set and not yet delivered, by its number. To move a "
            "reminder, cancel it and set it again."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "The reminder number, e.g. 12."}
            },
            "required": ["id"],
        },
    },
}


class ReminderTools(LocalToolset):
    """The three reminder tools over a :class:`Reminders` store, on owner turns."""

    definitions: list[dict[str, Any]] = [REMIND_SET_TOOL, REMINDERS_LIST_TOOL, REMINDER_CANCEL_TOOL]

    def __init__(
        self,
        store: Reminders,
        zone: ZoneInfo,
        default_time: str,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self._zone = zone
        self._default_time = default_time
        self._now = now

    @classmethod
    def from_config(
        cls, config: Config, sessions: async_sessionmaker[AsyncSession]
    ) -> ReminderTools:
        return cls(Reminders(sessions), ZoneInfo(config.ai.timezone), config.reminders.default_time)

    def offered(self, turn: TurnInput) -> bool:
        return owner_only(turn)

    async def call(
        self, name: str, raw_arguments: Any, attempt: AttemptContext | None = None
    ) -> ToolOutcome:
        arguments = parse_arguments(raw_arguments)
        if isinstance(arguments, str):
            return ToolOutcome(False, arguments)
        if name == REMIND_SET:
            return await self._set(arguments)
        if name == REMINDERS_LIST:
            return await self._list()
        if name == REMINDER_CANCEL:
            return await self._cancel(arguments)
        return ToolOutcome(False, f"Error: unknown tool {name!r}.")

    async def _set(self, arguments: dict[str, Any]) -> ToolOutcome:
        now = self._now()
        try:
            text = parse_text(arguments.get("text"))
            due = resolve(
                in_minutes=arguments.get("in_minutes"),
                at=arguments.get("at"),
                now=now,
                zone=self._zone,
                default_time=self._default_time,
            )
        except WhenError as exc:
            return ToolOutcome(False, str(exc))
        reminder_id = await self.store.set(text, due)
        log.info("reminder #%s set for %s", reminder_id, due.isoformat())
        # No number and no countdown: the model echoes this, and the owner
        # wants a plain confirmation (numbers appear only in the list).
        return ToolOutcome(
            True,
            f"Reminder set for {format_day_time(due, now, self._zone)}: {text}. "
            "Confirm it to the owner in your own words with this day and time; "
            "don't mention a reminder number.",
        )

    async def _list(self) -> ToolOutcome:
        now = self._now()
        rows = await self.store.pending()
        if not rows:
            return ToolOutcome(True, "There are no reminders set.")
        lines = [
            f"#{r.id} {format_when(r.due_at, self._zone)} ({format_in(r.due_at - now)}): {r.text}"
            for r in rows
        ]
        return ToolOutcome(True, "Reminders set, soonest first:\n" + "\n".join(lines))

    async def _cancel(self, arguments: dict[str, Any]) -> ToolOutcome:
        value = arguments.get("id")
        if isinstance(value, str) and value.strip().lstrip("#").isdigit():
            value = int(value.strip().lstrip("#"))
        if isinstance(value, bool) or not isinstance(value, int):
            return ToolOutcome(False, "Error: id must be a reminder number, e.g. 12.")
        outcome, row = await self.store.cancel(value, self._now())
        if outcome == "unknown" or row is None:
            return ToolOutcome(False, f"Error: there is no reminder #{value}.")
        when = format_when(row.due_at, self._zone)
        if outcome == "fired":
            return ToolOutcome(False, f"Error: reminder #{value} was already delivered ({when}).")
        if outcome == "cancelled_before":
            return ToolOutcome(False, f"Error: reminder #{value} was already cancelled.")
        log.info("reminder #%s cancelled", value)
        return ToolOutcome(True, f"Reminder #{value} cancelled (was set for {when}): {row.text}")
