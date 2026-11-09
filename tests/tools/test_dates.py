"""``alfred_date`` (change add-utility-tools)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from alfred.tools.base import AttemptContext, ToolOutcome, TurnInput
from alfred.tools.dates import DATE, DATE_TOOL, DateTools

LISBON = ZoneInfo("Europe/Lisbon")
NOW = datetime(2026, 11, 9, 14, 5, tzinfo=UTC)  # Monday, 14:05 in Lisbon (WET)
OWNER = TurnInput("owner", "how many days until Christmas?", 1)


async def call(arguments: dict[str, object], now: datetime = NOW) -> ToolOutcome:
    tools = DateTools(LISBON, now=lambda: now)
    return await tools.call(DATE, json.dumps(arguments), AttemptContext(OWNER))


async def result(now: datetime = NOW, **arguments: object) -> str:
    outcome = await call(arguments, now)
    assert outcome.ok, outcome.text
    return outcome.text


async def error(**arguments: object) -> str:
    outcome = await call(arguments)
    assert not outcome.ok, outcome.text
    assert outcome.text.startswith("Error: ")
    return outcome.text


# --- offered -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "turn",
    [OWNER, TurnInput("ask", "x", 2, "disk-check"), TurnInput("reminder", "x", 3)],
)
async def test_offered_on_every_turn(turn: TurnInput) -> None:
    tools = DateTools(LISBON)
    assert await tools.open(turn) is tools
    assert tools.definitions == [DATE_TOOL]


def test_definition() -> None:
    function = DATE_TOOL["function"]
    assert function["description"].startswith("Use for any date calculation")
    assert function["parameters"]["properties"]["op"]["enum"] == [
        "between",
        "shift",
        "weekday",
        "now_in",
    ]


# --- dates accepted ------------------------------------------------------------------


async def test_today_after_local_midnight() -> None:
    # 00:30 on Tuesday 10 November in Lisbon.
    late = datetime(2026, 11, 10, 0, 30, tzinfo=UTC)
    assert await result(late, op="weekday", date="today") == "10 November 2026 is a Tuesday."


async def test_impossible_date() -> None:
    assert await error(op="weekday", date="2027-02-30") == (
        "Error: 2027-02-30 is not a valid date."
    )


async def test_unreadable_date() -> None:
    assert "YYYY-MM-DD" in await error(op="weekday", date="next Friday")


# --- between ---------------------------------------------------------------------------


async def test_days_until_christmas() -> None:
    assert await result(op="between", **{"from": "today", "to": "2026-12-25"}) == (
        "Friday 25 December 2026 is 46 days (6 weeks and 4 days) after Monday 9 November 2026."
    )


async def test_date_in_the_past() -> None:
    assert await result(op="between", **{"from": "today", "to": "2026-10-01"}) == (
        "Thursday 1 October 2026 was 39 days (5 weeks and 4 days) before Monday 9 November 2026."
    )


async def test_earlier_future_date_is_not_was() -> None:
    assert await result(op="between", **{"from": "2027-01-10", "to": "2027-01-08"}) == (
        "Friday 8 January 2027 is 2 days before Sunday 10 January 2027."
    )


@pytest.mark.parametrize(
    ("to", "expected"),
    [
        ("2026-11-09", "Monday 9 November 2026 and Monday 9 November 2026 are the same day."),
        ("2026-11-10", "Tuesday 10 November 2026 is 1 day after Monday 9 November 2026."),
        (
            "2026-11-23",
            "Monday 23 November 2026 is 14 days (2 weeks) after Monday 9 November 2026.",
        ),
        (
            "2026-11-17",
            "Tuesday 17 November 2026 is 8 days (1 week and 1 day) after Monday 9 November 2026.",
        ),
    ],
)
async def test_day_counts(to: str, expected: str) -> None:
    assert await result(op="between", **{"from": "today", "to": to}) == expected


async def test_between_needs_both_dates() -> None:
    assert await error(op="between", **{"from": "today"}) == (
        "Error: between needs from and to (YYYY-MM-DD or today)."
    )


# --- shift ------------------------------------------------------------------------------


async def test_a_hundred_days_on() -> None:
    assert await result(op="shift", date="2026-11-09", days=100) == (
        "Monday 9 November 2026 +100 days is Wednesday 17 February 2027."
    )


async def test_month_end() -> None:
    assert await result(op="shift", date="2027-01-31", months=1) == (
        "Sunday 31 January 2027 +1 month is Sunday 28 February 2027."
    )


async def test_leap_year_month_end() -> None:
    assert await result(op="shift", date="2027-01-31", months=13) == (
        "Sunday 31 January 2027 +13 months is Tuesday 29 February 2028."
    )


async def test_mixed_and_negative() -> None:
    assert await result(op="shift", date="today", weeks=-2, days="3") == (
        "Monday 9 November 2026 -2 weeks, +3 days is Thursday 29 October 2026."
    )


async def test_months_back_across_a_year() -> None:
    assert await result(op="shift", date="2027-01-15", months=-2) == (
        "Friday 15 January 2027 -2 months is Sunday 15 November 2026."
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"date": "9999-12-31", "days": 1},
        {"date": "0001-01-01", "days": -1},
        {"date": "2026-11-09", "months": 120000},
        {"date": "2026-11-09", "days": 10**12},
    ],
)
async def test_outside_the_calendar(arguments: dict[str, object]) -> None:
    assert await error(op="shift", **arguments) == (
        "Error: the result is outside the years 1 to 9999."
    )


async def test_shift_needs_an_amount() -> None:
    assert "shift needs days, weeks or months" in await error(op="shift", date="today")


@pytest.mark.parametrize("days", [1.5, "two", True])
async def test_amounts_are_whole(days: object) -> None:
    assert await error(op="shift", date="today", days=days) == (
        "Error: days must be a whole number."
    )


# --- weekday ----------------------------------------------------------------------------


async def test_weekday() -> None:
    assert await result(op="weekday", date="2027-03-03") == "3 March 2027 is a Wednesday."


async def test_weekday_needs_a_date() -> None:
    assert await error(op="weekday") == "Error: weekday needs date (YYYY-MM-DD or today)."


# --- now_in ----------------------------------------------------------------------------


async def test_time_in_tokyo() -> None:
    assert await result(op="now_in", zone="Asia/Tokyo") == (
        "It is 23:05 on Monday 9 November 2026 in Asia/Tokyo, 9 hours ahead of the owner."
    )


async def test_half_hour_offset() -> None:
    assert await result(op="now_in", zone="Asia/Kolkata") == (
        "It is 19:35 on Monday 9 November 2026 in Asia/Kolkata, "
        "5 hours 30 minutes ahead of the owner."
    )


async def test_behind_the_owner() -> None:
    assert await result(op="now_in", zone="America/New_York") == (
        "It is 09:05 on Monday 9 November 2026 in America/New_York, 5 hours behind the owner."
    )


async def test_same_offset() -> None:
    assert await result(op="now_in", zone="Europe/London") == (
        "It is 14:05 on Monday 9 November 2026 in Europe/London, the same time as the owner."
    )


async def test_owners_own_time() -> None:
    assert await result(op="now_in") == "It is 14:05 on Monday 9 November 2026 (the owner's time)."


@pytest.mark.parametrize("zone", ["Tokyo", "../etc/passwd", 9])
async def test_unknown_zone(zone: object) -> None:
    text = await error(op="now_in", zone=zone)
    assert "use an IANA name such as Asia/Tokyo" in text


# --- bad calls -----------------------------------------------------------------------------


@pytest.mark.parametrize("arguments", [{"op": "diff"}, {}])
async def test_unknown_op(arguments: dict[str, object]) -> None:
    outcome = await call(arguments)
    assert not outcome.ok
    assert outcome.text == "Error: op must be one of between, shift, weekday, now_in."


async def test_invalid_json() -> None:
    outcome = await DateTools(LISBON).call(DATE, "{nope", AttemptContext(OWNER))
    assert not outcome.ok and "not valid JSON" in outcome.text
