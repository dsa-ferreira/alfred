from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from alfred.times import (
    WhenError,
    format_day_time,
    format_in,
    format_now,
    parse_date,
    seconds_to_next_check,
)

LISBON = ZoneInfo("Europe/Lisbon")
DUE = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)


def at_lisbon(*args: int) -> datetime:
    return datetime(*args, tzinfo=LISBON)  # type: ignore[misc]


# --- Formatting --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "in less than a minute"),
        (10, "in 1 min"),  # rounded up, like the clocks: 13:55:50 to 13:56
        (60, "in 1 min"),
        (83, "in 2 min"),
        (2 * 3600 + 13 * 60, "in 2 h 13 min"),
        (14 * 3600, "in 14 h"),
        (86400 + 60, "in 1 day 1 min"),
        (3 * 86400 + 3600, "in 3 days 1 h"),
    ],
)
def test_format_in(seconds: int, expected: str) -> None:
    assert format_in(timedelta(seconds=seconds)) == expected


def test_format_now() -> None:
    due_at = at_lisbon(2026, 11, 9, 17, 0)
    assert format_now(at_lisbon(2026, 11, 9, 19, 40), due_at, LISBON) == "19:40"
    assert (
        format_now(at_lisbon(2026, 11, 12, 8, 5), due_at, LISBON)
        == "Thursday 12 November 2026, 08:05"
    )


@pytest.mark.parametrize(
    ("due_at", "expected"),
    [
        (at_lisbon(2026, 11, 9, 13, 57), "today at 13:57"),
        (at_lisbon(2026, 11, 10, 5, 0), "tomorrow at 05:00"),
        (at_lisbon(2026, 11, 13, 9, 30), "on Friday at 09:30"),
        (at_lisbon(2026, 11, 16, 9, 0), "on Monday 16 November at 09:00"),
        (at_lisbon(2027, 1, 4, 9, 0), "on Monday 4 January 2027 at 09:00"),
    ],
)
def test_format_day_time(due_at: datetime, expected: str) -> None:
    assert format_day_time(due_at, at_lisbon(2026, 11, 9, 13, 55, 37), LISBON) == expected


def test_format_day_time_after_midnight_utc() -> None:
    # 00:30 in Tokyo is still the previous day in UTC: "today" follows the owner's zone.
    tokyo = ZoneInfo("Asia/Tokyo")
    now = datetime(2026, 11, 9, 0, 30, tzinfo=tokyo)
    assert format_day_time(datetime(2026, 11, 9, 23, 0, tzinfo=tokyo), now, tokyo) == (
        "today at 23:00"
    )


# --- parse_date (change add-utility-tools) ----------------------------------------


def test_parse_date() -> None:
    now = datetime(2026, 11, 9, 14, 5, tzinfo=UTC)
    assert parse_date("2027-03-03", now=now, zone=LISBON).isoformat() == "2027-03-03"
    assert parse_date(" today ", now=now, zone=LISBON).isoformat() == "2026-11-09"
    assert parse_date("Today", now=now, zone=LISBON).isoformat() == "2026-11-09"


def test_today_just_after_local_midnight() -> None:
    # 00:30 on 10 November in Lisbon is still 9 November in New York.
    now = datetime(2026, 11, 10, 0, 30, tzinfo=UTC)
    assert parse_date("today", now=now, zone=LISBON).isoformat() == "2026-11-10"
    assert parse_date("today", now=now, zone=ZoneInfo("America/New_York")).isoformat() == (
        "2026-11-09"
    )


FORMS = '"YYYY-MM-DD" or "today"'


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("2027-02-30", "Error: 2027-02-30 is not a valid date."),
        (
            "next friday",
            'Error: can\'t read the date \'next friday\'. Use "YYYY-MM-DD" or "today".',
        ),
        ("3/3/2027", 'Error: can\'t read the date \'3/3/2027\'. Use "YYYY-MM-DD" or "today".'),
        (20270303, f"Error: a date must be text: {FORMS}."),
    ],
)
def test_bad_dates(value: object, message: str) -> None:
    with pytest.raises(WhenError) as caught:
        parse_date(value, now=datetime(2026, 11, 9, tzinfo=UTC), zone=LISBON)
    assert str(caught.value) == message


# --- The loops' clock ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("second", "interval", "expected"),
    [
        (20.0, 30, 10),  # to :30
        (30.0, 30, 30),  # just checked at :30, to :00
        (59.9995, 30, 0.0005),  # woke a hair early: check again at :00
        (0.0, 30, 30),
        (17.0, 45, 28),  # to :45
        (50.0, 45, 10),  # to :00, never skipping the minute
        (5.0, 60, 55),
        (12.5, 10, 7.5),
    ],
)
def test_seconds_to_next_check(second: float, interval: float, expected: float) -> None:
    now = DUE + timedelta(seconds=second)
    assert seconds_to_next_check(now, interval) == pytest.approx(expected)
