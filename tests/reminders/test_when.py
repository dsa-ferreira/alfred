from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from alfred.reminders.when import (
    WhenError,
    format_day_time,
    format_in,
    format_now,
    format_when,
    parse_date,
    parse_text,
    resolve,
)

LISBON = ZoneInfo("Europe/Lisbon")


def at_lisbon(*args: int) -> datetime:
    return datetime(*args, tzinfo=LISBON)  # type: ignore[misc]


def due(now: datetime, *, in_minutes: object = None, at: object = None, default: str = "09:00"):
    return resolve(in_minutes=in_minutes, at=at, now=now, zone=LISBON, default_time=default)


# --- Setting a reminder ------------------------------------------------------------


def test_relative_time() -> None:
    now = at_lisbon(2026, 11, 9, 16, 47)
    result = due(now, in_minutes=90)
    assert result == at_lisbon(2026, 11, 9, 18, 17)
    assert result.tzinfo == UTC
    assert format_when(result, LISBON) == "Monday 9 November 2026, 18:17"
    assert format_in(result - now) == "in 1 h 30 min"


@pytest.mark.parametrize(("in_minutes", "at"), [(15, "17:00"), (None, None)])
def test_both_or_neither(in_minutes: object, at: object) -> None:
    with pytest.raises(WhenError, match="exactly one"):
        due(at_lisbon(2026, 11, 9, 16, 47), in_minutes=in_minutes, at=at)


@pytest.mark.parametrize("value", [0, -5, 1.5, True, "soon", 10**7])
def test_bad_minutes(value: object) -> None:
    with pytest.raises(WhenError, match="in_minutes|one year"):
        due(at_lisbon(2026, 11, 9, 16, 47), in_minutes=value)


def test_relative_time_drops_the_seconds() -> None:
    # Set at 13:55:37 for "in 2 minutes": due at the 13:57 the owner is told.
    now = at_lisbon(2026, 11, 9, 13, 55, 37)
    result = due(now, in_minutes=2)
    assert result == at_lisbon(2026, 11, 9, 13, 57)
    assert format_when(result, LISBON) == "Monday 9 November 2026, 13:57"
    assert format_in(result - now) == "in 2 min"


def test_one_minute_is_still_ahead() -> None:
    now = at_lisbon(2026, 11, 9, 13, 55, 59)
    assert due(now, in_minutes=1) == at_lisbon(2026, 11, 9, 13, 56)


def test_clock_time_countdown_matches_the_clocks() -> None:
    now = at_lisbon(2026, 11, 9, 14, 47, 20)
    assert format_in(due(now, at="17:00") - now) == "in 2 h 13 min"


@pytest.mark.parametrize("value", [15.0, "15"])
def test_lenient_minutes(value: object) -> None:
    now = at_lisbon(2026, 11, 9, 16, 47)
    assert due(now, in_minutes=value) == now + timedelta(minutes=15)  # whole minute already


@pytest.mark.parametrize("value", ["", "   ", None, 12])
def test_empty_text(value: object) -> None:
    with pytest.raises(WhenError, match="text"):
        parse_text(value)


def test_text_is_stripped_and_capped() -> None:
    assert parse_text("  call mum ") == "call mum"
    with pytest.raises(WhenError, match="too long"):
        parse_text("x" * 501)


# --- Clock time without a date -----------------------------------------------------


def test_clock_still_ahead_today() -> None:
    assert due(at_lisbon(2026, 11, 9, 14, 47), at="17:00") == at_lisbon(2026, 11, 9, 17, 0)


def test_clock_already_passed_today() -> None:
    assert due(at_lisbon(2026, 11, 9, 18, 5), at="17:00") == at_lisbon(2026, 11, 10, 17, 0)


def test_clock_equal_to_now_is_tomorrow() -> None:
    assert due(at_lisbon(2026, 11, 9, 17, 0), at="17:00") == at_lisbon(2026, 11, 10, 17, 0)


def test_midnight_rollover() -> None:
    now = at_lisbon(2026, 12, 31, 23, 50)
    result = due(now, at="00:10")
    assert result == at_lisbon(2027, 1, 1, 0, 10)
    assert format_in(result - now) == "in 20 min"
    assert due(now, in_minutes=30) == at_lisbon(2027, 1, 1, 0, 20)


# --- Date with or without a clock time ---------------------------------------------


def test_date_and_time() -> None:
    result = due(at_lisbon(2026, 11, 9, 14, 47), at="2026-11-13 09:30")
    assert format_when(result, LISBON) == "Friday 13 November 2026, 09:30"


def test_date_only_uses_default_time() -> None:
    result = due(at_lisbon(2026, 11, 9, 14, 47), at="2026-11-13")
    assert format_when(result, LISBON) == "Friday 13 November 2026, 09:00"


def test_date_only_uses_configured_default_time() -> None:
    result = due(at_lisbon(2026, 11, 9, 14, 47), at="2026-11-13", default="07:45")
    assert format_when(result, LISBON) == "Friday 13 November 2026, 07:45"


def test_in_the_past() -> None:
    with pytest.raises(WhenError, match="already passed"):
        due(at_lisbon(2026, 11, 9, 14, 47), at="2026-11-08 10:00")


def test_todays_default_time_passed() -> None:
    with pytest.raises(WhenError, match="already passed"):
        due(at_lisbon(2026, 11, 9, 11, 0), at="2026-11-09")


@pytest.mark.parametrize("value", ["five pm", "17h", "2026-13-01", "25:00", "2026-02-30 10:00"])
def test_unreadable_time(value: str) -> None:
    with pytest.raises(WhenError) as exc:
        due(at_lisbon(2026, 11, 9, 14, 47), at=value)
    message = str(exc.value)
    assert "HH:MM" in message or "not a valid" in message


def test_unreadable_time_names_the_forms() -> None:
    with pytest.raises(WhenError, match='"HH:MM".*"YYYY-MM-DD HH:MM".*"YYYY-MM-DD"'):
        due(at_lisbon(2026, 11, 9, 14, 47), at="five pm")


# --- DST ---------------------------------------------------------------------------


def test_spring_forward_gap_moves_forward() -> None:
    # Lisbon, 29 March 2026: 01:00 WET jumps to 02:00 WEST; 01:30 does not exist.
    result = due(at_lisbon(2026, 3, 28, 12, 0), at="2026-03-29 01:30")
    assert result == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    assert format_when(result, LISBON) == "Sunday 29 March 2026, 02:30"


def test_autumn_repeat_takes_first_occurrence() -> None:
    # Lisbon, 25 October 2026: 02:00 WEST falls back to 01:00 WET; 01:30 happens twice.
    result = due(at_lisbon(2026, 10, 24, 12, 0), at="2026-10-25 01:30")
    assert result == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 01:30 WEST


def test_relative_time_across_dst() -> None:
    # In UTC: an hour of minutes is an hour, whatever the clocks do.
    now = at_lisbon(2026, 10, 25, 0, 30)
    result = due(now, in_minutes=120)
    assert result - now.astimezone(UTC) == timedelta(hours=2)
    assert format_when(result, LISBON) == "Sunday 25 October 2026, 01:30"


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
