"""Times as users read them, in alfred's time zone, and the clock the loops check on.

Shared by the agent (the time in the prompt and in history lines), the date
tool, reminders and the input request expiry loop.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DATE_FORMS = '"YYYY-MM-DD" or "today"'


class WhenError(ValueError):
    """The time can't be used; the message is written for the model."""


def format_when(moment: datetime, zone: ZoneInfo) -> str:
    """``Monday 9 November 2026, 17:00`` in alfred's timezone."""
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


def format_now(now: datetime, due: datetime, zone: ZoneInfo) -> str:
    """The current time for a reminder input: ``17:12`` on the due day, else in full."""
    if now.astimezone(zone).date() == due.astimezone(zone).date():
        return _format_clock(now, zone)
    return format_when(now, zone)


def format_in(delta: timedelta) -> str:
    """``in 2 h 13 min``: days, hours and minutes, zero parts dropped.

    Rounded up, so it matches the clock times the user sees: from 14:47:20
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


def parse_date(value: Any, *, now: datetime, zone: ZoneInfo) -> date:
    """A date as ``YYYY-MM-DD``, or ``today`` in ``zone`` at ``now``.

    Shared with ``alfred_date``. Raises :class:`WhenError` with a
    message for the model.
    """
    if not isinstance(value, str):
        raise WhenError(f"Error: a date must be text: {_DATE_FORMS}.")
    text = value.strip()
    if text.lower() == "today":
        return now.astimezone(zone).date()
    if match := DATE.fullmatch(text):
        return calendar_date(*match.group(1, 2, 3))
    raise WhenError(f"Error: can't read the date {value!r}. Use {_DATE_FORMS}.")


def calendar_date(year: str, month: str, day: str) -> date:
    """The date, or :class:`WhenError` if there is no such day."""
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        raise WhenError(f"Error: {year}-{month}-{day} is not a valid date.") from None


def seconds_to_next_check(now: datetime, interval_s: float) -> float:
    """Seconds from ``now`` to the next check: the next multiple of the interval within
    the minute, or the next whole minute."""
    into_minute = now.second + now.microsecond / 1_000_000
    step = min(interval_s, 60)
    following = min((math.floor(into_minute / step) + 1) * step, 60)
    return following - into_minute


def _format_clock(moment: datetime, zone: ZoneInfo) -> str:
    """``17:00`` in alfred's timezone."""
    return f"{moment.astimezone(zone):%H:%M}"
