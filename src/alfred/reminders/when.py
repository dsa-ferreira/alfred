"""When a reminder is due, and how times are shown (change add-reminders, design D2).

The model passes the time as the owner said it: ``in_minutes``, or ``at`` as
``HH:MM``, ``YYYY-MM-DD HH:MM`` or ``YYYY-MM-DD``. alfred resolves it in the
owner's timezone against an injected ``now``, so the model never does date
arithmetic and never needs the time of day.

Due times are whole minutes: ``in_minutes`` drops the seconds, so a
reminder fires at the minute the owner is told, and the due-reminder loop
checks on the minute.

Local times are built with ``fold=0``. A time in a DST gap (it does not
exist) is moved forward by the gap through a round trip via UTC; an
ambiguous time (the autumn repeat) takes its first occurrence.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

MAX_MINUTES = 366 * 24 * 60  # about one year
MAX_TEXT_CHARS = 500
AT_FORMS = '"HH:MM" (24-hour), "YYYY-MM-DD HH:MM" or "YYYY-MM-DD"'

_CLOCK = re.compile(r"(\d{1,2}):(\d{2})")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DATE_CLOCK = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})")


class WhenError(ValueError):
    """The time can't be used; the message is written for the model."""


def parse_text(value: Any) -> str:
    """The reminder text, stripped. Raises :class:`WhenError` if blank or too long."""
    if not isinstance(value, str) or not value.strip():
        raise WhenError("Error: text is required: what to remind the owner of.")
    text = value.strip()
    if len(text) > MAX_TEXT_CHARS:
        raise WhenError(f"Error: text is too long (at most {MAX_TEXT_CHARS} characters).")
    return text


def resolve(
    *,
    in_minutes: Any = None,
    at: Any = None,
    now: datetime,
    zone: ZoneInfo,
    default_time: str,
) -> datetime:
    """The due time in UTC, strictly after ``now``.

    Exactly one of ``in_minutes`` and ``at`` must be given. Raises
    :class:`WhenError` with a message for the model.
    """
    if (in_minutes is None) == (at is None):
        raise WhenError("Error: pass exactly one of in_minutes or at.")
    now = now.astimezone(UTC)
    if in_minutes is not None:
        minutes = _whole_minutes(in_minutes)
        # Down to the minute shown to the owner: 13:55:37 + 2 min is 13:57:00.
        return (now + timedelta(minutes=minutes)).replace(second=0, microsecond=0)

    if not isinstance(at, str):
        raise WhenError(f"Error: at must be text in one of the forms {AT_FORMS}.")
    value = at.strip()
    local_now = now.astimezone(zone)

    if match := _CLOCK.fullmatch(value):
        clock = _clock(match.group(1), match.group(2))
        due = _local(local_now.date(), clock, zone)
        if due <= now:
            due = _local(local_now.date() + timedelta(days=1), clock, zone)
        return due

    if match := _DATE_CLOCK.fullmatch(value):
        day = _date(*match.group(1, 2, 3))
        due = _local(day, _clock(match.group(4), match.group(5)), zone)
    elif match := _DATE.fullmatch(value):
        day = _date(*match.group(1, 2, 3))
        hour, minute = default_time.split(":")
        due = _local(day, _clock(hour, minute), zone)
    else:
        raise WhenError(f"Error: can't read the time {value!r}. Use at as {AT_FORMS}.")

    if due <= now:
        raise WhenError(
            f"Error: {format_when(due, zone)} has already passed. "
            "Ask the owner for a time in the future."
        )
    if due - now > timedelta(minutes=MAX_MINUTES):
        raise WhenError("Error: a reminder can be at most one year ahead.")
    return due


def _whole_minutes(value: Any) -> int:
    minutes: int | None = None
    if isinstance(value, bool):
        minutes = None
    elif isinstance(value, int):
        minutes = value
    elif isinstance(value, float) and value.is_integer():
        minutes = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        minutes = int(value.strip())
    if minutes is None or minutes < 1:
        raise WhenError("Error: in_minutes must be a whole number of minutes, 1 or more.")
    if minutes > MAX_MINUTES:
        raise WhenError("Error: a reminder can be at most one year ahead.")
    return minutes


def _clock(hour: str, minute: str) -> time:
    h, m = int(hour), int(minute)
    if h > 23 or m > 59:
        raise WhenError(f"Error: {hour}:{minute} is not a valid 24-hour time.")
    return time(h, m)


def _date(year: str, month: str, day: str) -> date:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        raise WhenError(f"Error: {year}-{month}-{day} is not a valid date.") from None


def _local(day: date, clock: time, zone: ZoneInfo) -> datetime:
    """That wall-clock time in ``zone``, as UTC; a time in a DST gap moves forward by it."""
    return datetime.combine(day, clock, tzinfo=zone).astimezone(UTC)


# --- formatting ------------------------------------------------------------------


def format_when(moment: datetime, zone: ZoneInfo) -> str:
    """``Monday 9 November 2026, 17:00`` in the owner's timezone."""
    local = moment.astimezone(zone)
    return f"{local:%A} {local.day} {local:%B %Y}, {local:%H:%M}"


def format_day_time(moment: datetime, now: datetime, zone: ZoneInfo) -> str:
    """The time as a person says it, from ``now``: ``today at 13:57``,
    ``tomorrow at 05:00``, ``on Friday at 09:30`` within the week, else
    ``on Friday 20 November at 09:30`` (with the year when it isn't this one)."""
    local = moment.astimezone(zone)
    days = (local.date() - now.astimezone(zone).date()).days
    clock = f"{local:%H:%M}"
    if days == 0:
        return f"today at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    if 1 < days < 7:
        return f"on {local:%A} at {clock}"
    year = f" {local.year}" if local.year != now.astimezone(zone).year else ""
    return f"on {local:%A} {local.day} {local:%B}{year} at {clock}"


def format_clock(moment: datetime, zone: ZoneInfo) -> str:
    """``17:00`` in the owner's timezone."""
    return f"{moment.astimezone(zone):%H:%M}"


def format_now(now: datetime, due: datetime, zone: ZoneInfo) -> str:
    """The current time for a reminder input: ``17:12`` on the due day, else in full."""
    if now.astimezone(zone).date() == due.astimezone(zone).date():
        return format_clock(now, zone)
    return format_when(now, zone)


def format_in(delta: timedelta) -> str:
    """``in 2 h 13 min``: days, hours and minutes, zero parts dropped.

    Rounded up, so it matches the clock times the owner sees: from 14:47:20
    to 17:00 is "in 2 h 13 min", as from 14:47 to 17:00.
    """
    minutes = math.ceil(delta.total_seconds() / 60)
    if minutes < 1:
        return "in less than a minute"
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} day" if days == 1 else f"{days} days")
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    return "in " + " ".join(parts)
