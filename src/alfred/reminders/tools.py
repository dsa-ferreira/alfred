"""``alfred_remind_set``, ``alfred_reminders_list`` and ``alfred_reminder_cancel``.

All three are offered in chat turns (change add-reminders, design D1), and
only ``alfred_reminders_list`` in reminder turns, so a briefing can include
today's reminders but a reminder can't set or cancel reminders (change
add-recurring-reminders, design D4); always subject to the user's
allow-list. alfred resolves the time itself (``when``), so the result
tells the model, and through it the user, the exact time and how far away it
is. Every failure becomes an error result for the model.

A reminder is set by the turn's user for themselves, or ``for`` other
configured users (change add-multiple-users, design D7). Listing and
cancelling see only the reminders the turn's user set or is reminded of.

A reminder may repeat (``repeat``, ``days``, ``at`` as ``HH:MM``) and may be
quiet (``quiet_ok``): its turn may then stay silent when there is nothing to
tell. One number covers a whole series.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.config import Config, Users
from alfred.reminders.store import ReminderRow, Reminders
from alfred.reminders.when import (
    DAY_NAMES,
    REPEATS,
    WhenError,
    format_day_time,
    format_in,
    format_when,
    next_occurrence,
    parse_rule,
    parse_text,
    resolve,
)
from alfred.tools.base import (
    AttemptContext,
    LocalToolset,
    OpenToolset,
    ToolOutcome,
    TurnInput,
    parse_arguments,
)

log = logging.getLogger(__name__)

_REMIND_SET = "alfred_remind_set"
_REMINDERS_LIST = "alfred_reminders_list"
_REMINDER_CANCEL = "alfred_reminder_cancel"
NAMES = frozenset({_REMIND_SET, _REMINDERS_LIST, _REMINDER_CANCEL})


_REMINDERS_LIST_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _REMINDERS_LIST,
        "description": (
            "List the reminders that are set and not yet delivered, soonest first, with their "
            "numbers. Not the to-do list."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

_REMINDER_CANCEL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _REMINDER_CANCEL,
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
    """The three reminder tools over a :class:`Reminders` store: all of them on chat
    turns, only the list on reminder turns, none on ``/ask`` turns."""

    def __init__(
        self,
        store: Reminders,
        zone: ZoneInfo,
        default_time: str,
        users: Users,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self._zone = zone
        self._default_time = default_time
        self._users = users
        self._now = now
        self.definitions = [
            _remind_set_tool(users.names),
            _REMINDERS_LIST_TOOL,
            _REMINDER_CANCEL_TOOL,
        ]
        self._list_only = _ListOnly(self)

    @classmethod
    def from_config(
        cls, config: Config, sessions: async_sessionmaker[AsyncSession]
    ) -> ReminderTools:
        return cls(
            Reminders(sessions, ZoneInfo(config.ai.timezone)),
            ZoneInfo(config.ai.timezone),
            config.reminders.default_time,
            config.users,
        )

    async def open(self, turn: TurnInput) -> OpenToolset | None:
        if turn.kind == "chat":
            return self
        if turn.kind == "reminder":
            return self._list_only
        return None

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        arguments = parse_arguments(raw_arguments)
        if isinstance(arguments, str):
            return ToolOutcome(False, arguments)
        user = attempt.turn.user.name
        if name == _REMIND_SET:
            return await self._set(user, arguments)
        if name == _REMINDERS_LIST:
            return await self._list(user)
        if name == _REMINDER_CANCEL:
            return await self._cancel(user, arguments)
        return ToolOutcome(False, f"Error: unknown tool {name!r}.")

    def _recipients(self, user: str, value: Any) -> list[str]:
        """The users ``for`` names, in order without repeats; ``user`` alone by default.

        Raises :class:`WhenError` with the message for the model.
        """
        if value is None or value == "" or value == []:
            return [user]
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise WhenError('Error: for must be a list of user names, e.g. ["ana"].')
        names = list(dict.fromkeys(v.strip().lower() for v in value))
        unknown = [n for n in names if self._users.get(n) is None]
        if unknown:
            who = ", ".join(repr(n) for n in unknown)
            raise WhenError(
                f"Error: there is no user {who}. The users who can be reminded are: "
                f"{', '.join(self._users.names)}."
            )
        return names

    async def _set(self, user: str, arguments: dict[str, Any]) -> ToolOutcome:
        now = self._now()
        try:
            recipients = self._recipients(user, arguments.get("for"))
            text = parse_text(arguments.get("text"))
            quiet_ok = _quiet_ok(arguments.get("quiet_ok"))
            rule = parse_rule(
                repeat=arguments.get("repeat"),
                days=arguments.get("days"),
                at=arguments.get("at"),
                in_minutes=arguments.get("in_minutes"),
            )
            if rule is not None:
                due = next_occurrence(rule.days, rule.clock, now, self._zone)
            else:
                due = resolve(
                    in_minutes=arguments.get("in_minutes"),
                    at=arguments.get("at"),
                    now=now,
                    zone=self._zone,
                    default_time=self._default_time,
                )
        except WhenError as exc:
            return ToolOutcome(False, str(exc))
        reminder_id = await self.store.set(
            text, due, user, recipients, rule=rule, quiet_ok=quiet_ok
        )
        log.info(
            "reminder #%s set by %s for %s at %s%s%s",
            reminder_id,
            user,
            ", ".join(recipients),
            due.isoformat(),
            f", {rule.label()}" if rule else "",
            ", quiet" if quiet_ok else "",
        )
        # No number and no countdown: the model echoes this, and the user
        # wants a plain confirmation (numbers appear only in the list).
        when = format_day_time(due, now, self._zone)
        when = f"{rule.label()}, starting {when}" if rule else f"for {when}"
        if recipients == [user]:
            return ToolOutcome(
                True,
                f"Reminder set {when}: {text}. "
                "Confirm it to the user in your own words with this day and time; "
                "don't mention a reminder number.",
            )
        return ToolOutcome(
            True,
            f"Reminder set {when}, to remind {', '.join(recipients)}: {text}. Confirm it to "
            "the user in your own words with this day and time and who will be reminded; "
            "don't mention a reminder number.",
        )

    async def _list(self, user: str) -> ToolOutcome:
        now = self._now()
        rows = await self.store.pending(user)
        if not rows:
            return ToolOutcome(True, "There are no reminders set.")
        lines = [
            f"#{r.id} {format_when(r.due_at, self._zone)} ({format_in(r.due_at - now)})"
            f"{_details(r)}: {r.text}{_who(r, user)}"
            for r in rows
        ]
        return ToolOutcome(True, "Reminders set, soonest first:\n" + "\n".join(lines))

    async def _cancel(self, user: str, arguments: dict[str, Any]) -> ToolOutcome:
        value = arguments.get("id")
        if isinstance(value, str) and value.strip().lstrip("#").isdigit():
            value = int(value.strip().lstrip("#"))
        if isinstance(value, bool) or not isinstance(value, int):
            return ToolOutcome(False, "Error: id must be a reminder number, e.g. 12.")
        outcome, row = await self.store.cancel(value, self._now(), user)
        if outcome == "unknown" or row is None:
            return ToolOutcome(False, f"Error: there is no reminder #{value}.")
        when = format_when(row.due_at, self._zone)
        if outcome == "fired":
            return ToolOutcome(False, f"Error: reminder #{value} was already delivered ({when}).")
        if outcome == "cancelled_before":
            return ToolOutcome(False, f"Error: reminder #{value} was already cancelled.")
        details = _details(row).removeprefix(", ")
        if row.rule is not None:
            when = f"{details}, next {when}"
        if outcome == "left":
            log.info("%s left reminder #%s", user, value)
            return ToolOutcome(
                True,
                f"You won't be reminded of #{value} any more (set for {when}): {row.text}; "
                f"it stays set for {', '.join(row.recipients)}.",
            )
        log.info("reminder #%s cancelled by %s", value, user)
        return ToolOutcome(
            True, f"Reminder #{value} cancelled (was set for {when}): {row.text}{_who(row, user)}"
        )


def _remind_set_tool(users: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _REMIND_SET,
            "description": (
                "Set a reminder: alfred messages the user about it at that time. Use for "
                "requests with a time or date ('remind me to call mum at five', 'in 20 "
                "minutes', 'on Friday'), and for requests to do something later or on a "
                "schedule ('every weekday at 8, brief me on the weather'): the text says what "
                "to do then, and it is not done now. If the user says 'task' or 'todo', or "
                "gives no time, use the to-do list tools instead. Pass the time as the user "
                "said it, as exactly one of in_minutes or at; never work out times yourself. "
                "Confirm to the user the time this tool returns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "What to remind them of, or what to do at that time, e.g. "
                            "'call mum' or 'brief me on the weather'."
                        ),
                    },
                    "in_minutes": {
                        "type": "integer",
                        "description": (
                            "Minutes from now, for 'in 20 minutes' or 'in 2 hours' (120)."
                        ),
                    },
                    "at": {
                        "type": "string",
                        "description": (
                            "A local time: 'HH:MM' (24-hour; the next time it comes round), "
                            "'YYYY-MM-DD HH:MM', or 'YYYY-MM-DD' (a date with no time). With "
                            "repeat, only 'HH:MM'."
                        ),
                    },
                    "repeat": {
                        "type": "string",
                        "enum": list(REPEATS),
                        "description": (
                            "For 'every day', 'every weekday', 'at weekends' or 'every Tuesday "
                            "and Friday' (weekly, with days). Needs at as HH:MM."
                        ),
                    },
                    "days": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(DAY_NAMES)},
                        "description": "Weekdays, only with repeat weekly, e.g. ['tue', 'fri'].",
                    },
                    "quiet_ok": {
                        "type": "boolean",
                        "description": (
                            "true when the user only wants to hear if there is something to "
                            "tell ('tell me if…', 'only if…')."
                        ),
                    },
                    "for": {
                        "type": "array",
                        "items": {"type": "string", "enum": users},
                        "description": (
                            "The users to remind, by name, for 'remind ana ...' or 'remind "
                            "us ...'. Leave out to remind only the user you are talking to."
                        ),
                    },
                },
                "required": ["text"],
            },
        },
    }


class _ListOnly(LocalToolset):
    """``alfred_reminders_list`` alone, for reminder turns (design D4)."""

    def __init__(self, tools: ReminderTools) -> None:
        self._tools = tools
        self.definitions = [_REMINDERS_LIST_TOOL]

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        if name != _REMINDERS_LIST:
            return ToolOutcome(False, f"Error: unknown tool {name!r}.")
        return await self._tools.call(name, raw_arguments, attempt)


def _quiet_ok(value: Any) -> bool:
    """``quiet_ok`` as a boolean. Raises :class:`WhenError` for anything else."""
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise WhenError("Error: quiet_ok must be true or false.")


def _details(row: ReminderRow) -> str:
    """``, every weekday at 08:00, may stay quiet``: the rule and quietness, if any."""
    parts = [row.rule.label()] if row.rule is not None else []
    if row.quiet_ok:
        parts.append("may stay quiet")
    return "".join(f", {p}" for p in parts)


def _who(row: ReminderRow, user: str) -> str:
    """`` (set by diogo; for ana)``: who set it and who it is for, unless only ``user``."""
    if row.only_for(user):
        return ""
    parts = [f"set by {row.created_by}"] if row.created_by != user else []
    parts.append("for " + ", ".join(row.recipients))
    return f" ({'; '.join(parts)})"
