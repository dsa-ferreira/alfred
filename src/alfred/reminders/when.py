"""When a reminder is due.

The model passes the time as the user said it: ``in_minutes``, or ``at`` as
``HH:MM``, ``YYYY-MM-DD HH:MM`` or ``YYYY-MM-DD``. alfred resolves it in
alfred's timezone against an injected ``now``, so the model never does date
arithmetic and never needs the time of day.

Due times are whole minutes: ``in_minutes`` drops the seconds, so a
reminder fires at the minute the user is told, and the due-reminder loop
checks on the minute.

Local times are built with ``fold=0``. A time in a DST gap (it does not
exist) is moved forward by the gap through a round trip via UTC; an
ambiguous time (the autumn repeat) takes its first occurrence.

A repeating reminder has a :class:`Rule`: the weekdays it falls on, as a
bitmask (Monday = bit 0), and a local wall time. Each occurrence is
worked out from the rule in the zone (:func:`next_occurrence`), never by
adding 24 hours, so 08:00 stays 08:00 across DST changes.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from alfred.times import DATE, WhenError, calendar_date, format_when

_MAX_MINUTES = 366 * 24 * 60  # about one year
_AT_FORMS = '"HH:MM" (24-hour), "YYYY-MM-DD HH:MM" or "YYYY-MM-DD"'

_CLOCK = re.compile(r"(\d{1,2}):(\d{2})")
_DATE_CLOCK = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})")


REPEATS = ("daily", "weekdays", "weekends", "weekly")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DAY_NAMES = tuple(d[:3] for d in _WEEKDAYS)  # "mon" ... "sun", as the tool offers them
_ALL_DAYS = 0b1111111
_MASKS = {"daily": _ALL_DAYS, "weekdays": 0b0011111, "weekends": 0b1100000}
_REPEAT_FORMS = "daily, weekdays, weekends, or weekly with days"


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
        # Down to the minute shown to the user: 13:55:37 + 2 min is 13:57:00.
        return (now + timedelta(minutes=minutes)).replace(second=0, microsecond=0)

    if not isinstance(at, str):
        raise WhenError(f"Error: at must be text in one of the forms {_AT_FORMS}.")
    value = at.strip()
    local_now = now.astimezone(zone)

    if match := _CLOCK.fullmatch(value):
        clock = _clock(match.group(1), match.group(2))
        due = _local(local_now.date(), clock, zone)
        if due <= now:
            due = _local(local_now.date() + timedelta(days=1), clock, zone)
        return due

    if match := _DATE_CLOCK.fullmatch(value):
        day = calendar_date(*match.group(1, 2, 3))
        due = _local(day, _clock(match.group(4), match.group(5)), zone)
    elif match := DATE.fullmatch(value):
        day = calendar_date(*match.group(1, 2, 3))
        hour, minute = default_time.split(":")
        due = _local(day, _clock(hour, minute), zone)
    else:
        raise WhenError(f"Error: can't read the time {value!r}. Use at as {_AT_FORMS}.")

    if due <= now:
        raise WhenError(
            f"Error: {format_when(due, zone)} has already passed. "
            "Ask the user for a time in the future."
        )
    if due - now > timedelta(minutes=_MAX_MINUTES):
        raise WhenError("Error: a reminder can be at most one year ahead.")
    return due


@dataclass(frozen=True)
class Rule:
    """When a reminder repeats: ``repeat`` names it, ``days`` is the weekday mask."""

    repeat: str
    days: int
    clock: time

    @property
    def clock_text(self) -> str:
        """``08:00``, as stored in ``repeat_time``."""
        return f"{self.clock:%H:%M}"

    @classmethod
    def stored(cls, repeat: str, days: int, clock_text: str) -> Rule:
        """The rule from a reminder row's ``repeat``, ``repeat_days`` and ``repeat_time``."""
        hour, minute = clock_text.split(":")
        return cls(repeat, days, time(int(hour), int(minute)))

    def label(self) -> str:
        return _format_rule(self.repeat, self.days, self.clock)


def parse_rule(
    *, repeat: Any = None, days: Any = None, at: Any = None, in_minutes: Any = None
) -> Rule | None:
    """The repeat rule of an ``alfred_remind_set`` call, or ``None`` for a one-shot.

    Raises :class:`WhenError` with a message for the model naming the
    accepted forms.
    """
    if repeat is None or repeat == "":
        if days not in (None, "", []):
            raise WhenError('Error: days needs repeat weekly, e.g. repeat weekly, days ["tue"].')
        return None
    if not isinstance(repeat, str) or repeat.strip().lower() not in REPEATS:
        raise WhenError(f"Error: repeat must be one of {', '.join(REPEATS)}.")
    name = repeat.strip().lower()
    if in_minutes is not None:
        raise WhenError(
            "Error: a repeating reminder takes at as HH:MM, not in_minutes. "
            "For a one-off reminder, leave repeat out."
        )
    if not isinstance(at, str) or not (match := _CLOCK.fullmatch(at.strip())):
        raise WhenError('Error: a repeating reminder takes at as "HH:MM" (24-hour), e.g. "08:00".')
    clock = _clock(match.group(1), match.group(2))
    if name == "weekly":
        mask = _days_mask(days)
    elif days not in (None, "", []):
        raise WhenError(f"Error: days is only for repeat weekly; {name} already says which days.")
    else:
        mask = _MASKS[name]
    return Rule(name, mask, clock)


def next_occurrence(days: int, clock: time, after: datetime, zone: ZoneInfo) -> datetime:
    """The first occurrence of the rule strictly after ``after``, in UTC.

    Eight days covers a weekly rule whose only day is today and has passed.
    """
    start = after.astimezone(zone).date()
    for offset in range(8):
        day = start + timedelta(days=offset)
        if days & (1 << day.weekday()):
            candidate = _local(day, clock, zone)
            if candidate > after:
                return candidate
    raise ValueError(f"no occurrence for the day mask {days}")


def latest_occurrence(
    days: int, clock: time, due: datetime, now: datetime, zone: ZoneInfo
) -> datetime:
    """The latest occurrence at or before ``now``, stepping on from ``due``.

    On time it is ``due`` itself; after an outage, the most recent missed one.
    """
    latest = due
    while (following := next_occurrence(days, clock, latest, zone)) <= now:
        latest = following
    return latest


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
    if minutes > _MAX_MINUTES:
        raise WhenError("Error: a reminder can be at most one year ahead.")
    return minutes


def _clock(hour: str, minute: str) -> time:
    h, m = int(hour), int(minute)
    if h > 23 or m > 59:
        raise WhenError(f"Error: {hour}:{minute} is not a valid 24-hour time.")
    return time(h, m)


def _local(day: date, clock: time, zone: ZoneInfo) -> datetime:
    """That wall-clock time in ``zone``, as UTC; a time in a DST gap moves forward by it."""
    return datetime.combine(day, clock, tzinfo=zone).astimezone(UTC)


def _days_mask(value: Any) -> int:
    if isinstance(value, str):
        value = [value]
    if not value:
        raise WhenError(
            'Error: repeat weekly needs days, e.g. ["tue", "fri"]. '
            f"Days are {', '.join(DAY_NAMES)}."
        )
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise WhenError('Error: days must be a list of day names, e.g. ["tue", "fri"].')
    mask = 0
    for raw in value:
        day = raw.strip().lower()
        index = next((i for i, d in enumerate(_WEEKDAYS) if day in (d, d[:3])), None)
        if index is None:
            raise WhenError(f"Error: {raw!r} is not a day. Days are {', '.join(DAY_NAMES)}.")
        mask |= 1 << index
    return mask


def _format_rule(repeat: str, days: int, clock: time) -> str:
    """``every weekday at 08:00``, ``every Tuesday and Friday at 20:00``."""
    at = f"at {clock:%H:%M}"
    if days == _ALL_DAYS:
        return f"every day {at}"
    if days == _MASKS["weekdays"]:
        return f"every weekday {at}"
    if days == _MASKS["weekends"]:
        return f"every weekend day {at}"
    names = [_WEEKDAYS[i].capitalize() for i in range(7) if days & (1 << i)]
    return f"every {_and(names)} {at}"


def _and(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"
