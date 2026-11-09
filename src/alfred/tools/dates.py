"""``alfred_date``: calendar arithmetic in the owner's timezone (change add-utility-tools).

The model gets weekdays and day counts wrong, so alfred works them out
(design D5). One tool with an ``op`` keeps the definition small:

```
between  from, to             -> days from one to the other (and weeks and days)
shift    date, days/weeks/months -> the date that many days, weeks, months on
weekday  date                 -> its weekday
now_in   zone?                -> the date and time there (the owner's, with no zone)
```

Dates are ``YYYY-MM-DD`` or ``today``, read by the reminders' ``parse_date``,
so both tools word a bad date the same way.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alfred.reminders.when import WhenError, parse_date
from alfred.tools.base import AttemptContext, LocalToolset, ToolOutcome, parse_arguments

DATE = "alfred_date"

OPS = ("between", "shift", "weekday", "now_in")

DATE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": DATE,
        "description": (
            "Use for any date calculation instead of working it out yourself: the days "
            "between two dates (between), a date plus or minus days, weeks or months "
            "(shift), the weekday of a date (weekday), and the current date and time in "
            "a timezone (now_in). Dates are YYYY-MM-DD or today, in the owner's timezone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": list(OPS)},
                "from": {"type": "string", "description": "between: the first date."},
                "to": {"type": "string", "description": "between: the second date."},
                "date": {"type": "string", "description": "shift and weekday: the date."},
                "days": {"type": "integer", "description": "shift: days to add (or subtract)."},
                "weeks": {"type": "integer", "description": "shift: weeks to add (or subtract)."},
                "months": {
                    "type": "integer",
                    "description": "shift: months to add (or subtract).",
                },
                "zone": {
                    "type": "string",
                    "description": (
                        "now_in: an IANA timezone such as Asia/Tokyo; leave out for the owner's."
                    ),
                },
            },
            "required": ["op"],
        },
    },
}

_WHOLE = re.compile(r"[+-]?\d+")


class DateError(ValueError):
    """The date calculation can't be done; the message is written for the model."""


def long_date(day: date) -> str:
    """``Friday 25 December 2026``, like the reminders' times."""
    return f"{day:%A} {day.day} {day:%B %Y}"


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def day_count(days: int) -> str:
    """``46 days (6 weeks and 4 days)``; under a week, just the days."""
    text = _plural(days, "day")
    weeks, rest = divmod(days, 7)
    if weeks == 0:
        return text
    if rest == 0:
        return f"{text} ({_plural(weeks, 'week')})"
    return f"{text} ({_plural(weeks, 'week')} and {_plural(rest, 'day')})"


def _whole(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise DateError(f"{name} must be a whole number.")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and _WHOLE.fullmatch(value.strip()):
        return int(value.strip())
    raise DateError(f"{name} must be a whole number.")


def add_months(day: date, months: int) -> date:
    """``day`` ``months`` on; a day the target month lacks becomes its last day."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    if not 1 <= year <= 9999:
        raise DateError("the result is outside the years 1 to 9999.")
    last = calendar.monthrange(year, month + 1)[1]
    return date(year, month + 1, min(day.day, last))


def offset_text(offset: timedelta) -> str:
    """``9 hours ahead of the owner``, ``5 hours 30 minutes behind the owner``."""
    minutes = int(offset.total_seconds()) // 60
    if minutes == 0:
        return "the same time as the owner"
    hours, rest = divmod(abs(minutes), 60)
    parts = [_plural(hours, "hour")] if hours else []
    if rest:
        parts.append(_plural(rest, "minute"))
    return f"{' '.join(parts)} {'ahead of' if minutes > 0 else 'behind'} the owner"


class DateTools(LocalToolset):
    """``alfred_date``, on every turn, in the owner's timezone."""

    definitions = [DATE_TOOL]

    def __init__(
        self, zone: ZoneInfo, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._zone = zone
        self._now = now

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        arguments = parse_arguments(raw_arguments)
        if isinstance(arguments, str):
            return ToolOutcome(False, arguments)
        try:
            return ToolOutcome(True, self.answer(arguments))
        except WhenError as exc:
            return ToolOutcome(False, str(exc))
        except DateError as exc:
            return ToolOutcome(False, f"Error: {exc}")

    def answer(self, arguments: dict[str, Any]) -> str:
        """The tool's answer. Raises :class:`DateError` or :class:`WhenError`."""
        now = self._now()
        match arguments.get("op"):
            case "between":
                return self._between(arguments, now)
            case "shift":
                return self._shift(arguments, now)
            case "weekday":
                day = self._date(arguments, "date", "weekday", now)
                return f"{day.day} {day:%B %Y} is a {day:%A}."
            case "now_in":
                return self._now_in(arguments.get("zone"), now)
            case _:
                raise DateError(f"op must be one of {', '.join(OPS)}.")

    def _date(self, arguments: dict[str, Any], key: str, op: str, now: datetime) -> date:
        if arguments.get(key) in (None, ""):
            needs = "from and to" if op == "between" else key
            raise DateError(f"{op} needs {needs} (YYYY-MM-DD or today).")
        return parse_date(arguments[key], now=now, zone=self._zone)

    def _between(self, arguments: dict[str, Any], now: datetime) -> str:
        start = self._date(arguments, "from", "between", now)
        end = self._date(arguments, "to", "between", now)
        days = (end - start).days
        if days == 0:
            return f"{long_date(start)} and {long_date(end)} are the same day."
        if days > 0:
            return f"{long_date(end)} is {day_count(days)} after {long_date(start)}."
        today = now.astimezone(self._zone).date()
        verb = "was" if end < today else "is"
        return f"{long_date(end)} {verb} {day_count(-days)} before {long_date(start)}."

    def _shift(self, arguments: dict[str, Any], now: datetime) -> str:
        start = self._date(arguments, "date", "shift", now)
        amounts = {
            unit: _whole(arguments[unit], unit)
            for unit in ("months", "weeks", "days")
            if arguments.get(unit) not in (None, "")
        }
        if not amounts:
            raise DateError("shift needs days, weeks or months to add (negative to subtract).")
        try:
            result = add_months(start, amounts.get("months", 0))
            result += timedelta(days=amounts.get("days", 0) + 7 * amounts.get("weeks", 0))
        except (OverflowError, ValueError):
            raise DateError("the result is outside the years 1 to 9999.") from None
        steps = ", ".join(
            f"{n:+d} {unit if abs(n) != 1 else unit[:-1]}" for unit, n in amounts.items()
        )
        return f"{long_date(start)} {steps} is {long_date(result)}."

    def _now_in(self, zone_name: Any, now: datetime) -> str:
        if zone_name in (None, ""):
            local = now.astimezone(self._zone)
            return f"It is {local:%H:%M} on {long_date(local.date())} (the owner's time)."
        try:
            if not isinstance(zone_name, str):
                raise ValueError(zone_name)
            zone = ZoneInfo(zone_name.strip())
        except (ZoneInfoNotFoundError, ValueError):
            raise DateError(
                f"unknown timezone {zone_name!r}: use an IANA name such as Asia/Tokyo."
            ) from None
        local = now.astimezone(zone)
        owner = now.astimezone(self._zone)
        offset = (local.utcoffset() or timedelta()) - (owner.utcoffset() or timedelta())
        return (
            f"It is {local:%H:%M} on {long_date(local.date())} in {zone_name.strip()}, "
            f"{offset_text(offset)}."
        )
