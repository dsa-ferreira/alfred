from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.turn import TurnInput
from alfred.reminders.store import Reminders
from alfred.reminders.tools import (
    NAMES,
    REMIND_SET,
    REMINDER_CANCEL,
    REMINDERS_LIST,
    ReminderTools,
)

LISBON = ZoneInfo("Europe/Lisbon")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 11, 9, 16, 47, tzinfo=LISBON))


@pytest.fixture
def tools(sessions: async_sessionmaker[AsyncSession], clock: Clock) -> ReminderTools:
    return ReminderTools(Reminders(sessions), LISBON, "09:00", now=clock)


def test_definitions() -> None:
    names = {d["function"]["name"] for d in ReminderTools.definitions}
    assert names == NAMES
    set_description = next(
        d["function"]["description"]
        for d in ReminderTools.definitions
        if d["function"]["name"] == REMIND_SET
    )
    # The reminder vs to-do split is repeated where the model chooses.
    assert "'task' or 'todo'" in set_description
    assert "to-do list" in set_description


async def test_relative_time(tools: ReminderTools) -> None:
    outcome = await tools.call(REMIND_SET, {"text": "check the oven", "in_minutes": 90})
    assert outcome.ok
    assert outcome.text.startswith("Reminder set for today at 18:17: check the oven. ")
    # No number and no countdown for the model to repeat.
    assert "#" not in outcome.text and "in 1 h" not in outcome.text
    assert "don't mention a reminder number" in outcome.text
    [row] = await tools.store.pending()
    assert row.due_at == datetime(2026, 11, 9, 18, 17, tzinfo=UTC)


async def test_arguments_as_json_text(tools: ReminderTools) -> None:
    outcome = await tools.call(REMIND_SET, json.dumps({"text": "call mum", "at": "17:00"}))
    assert outcome.ok
    assert outcome.text.startswith("Reminder set for today at 17:00: call mum.")


@pytest.mark.parametrize(
    "arguments",
    [
        {"text": "call mum", "in_minutes": 15, "at": "17:00"},
        {"text": "call mum"},
    ],
)
async def test_both_or_neither(tools: ReminderTools, arguments: dict[str, object]) -> None:
    outcome = await tools.call(REMIND_SET, arguments)
    assert not outcome.ok
    assert "exactly one" in outcome.text
    assert await tools.store.pending() == []


@pytest.mark.parametrize("text", ["", "   "])
async def test_empty_text(tools: ReminderTools, text: str) -> None:
    outcome = await tools.call(REMIND_SET, {"text": text, "in_minutes": 5})
    assert not outcome.ok
    assert outcome.text.startswith("Error:")
    assert await tools.store.pending() == []


async def test_in_the_past(tools: ReminderTools) -> None:
    outcome = await tools.call(REMIND_SET, {"text": "x", "at": "2026-11-08 10:00"})
    assert not outcome.ok
    assert "already passed" in outcome.text
    assert await tools.store.pending() == []


async def test_unreadable_time(tools: ReminderTools) -> None:
    outcome = await tools.call(REMIND_SET, {"text": "x", "at": "five pm"})
    assert not outcome.ok
    assert '"HH:MM"' in outcome.text and '"YYYY-MM-DD"' in outcome.text


# --- Listing reminders --------------------------------------------------------------


async def test_list_two_reminders(tools: ReminderTools) -> None:
    await tools.call(REMIND_SET, {"text": "check the oven", "in_minutes": 90})
    await tools.call(REMIND_SET, {"text": "call mum", "at": "17:00"})
    outcome = await tools.call(REMINDERS_LIST, {})
    assert outcome.ok
    assert outcome.text.splitlines()[1:] == [
        "#2 Monday 9 November 2026, 17:00 (in 13 min): call mum",
        "#1 Monday 9 November 2026, 18:17 (in 1 h 30 min): check the oven",
    ]


async def test_list_nothing_set(tools: ReminderTools) -> None:
    outcome = await tools.call(REMINDERS_LIST, None)
    assert outcome.ok
    assert outcome.text == "There are no reminders set."


# --- Cancelling a reminder ----------------------------------------------------------


async def test_cancel_a_set_reminder(tools: ReminderTools) -> None:
    await tools.call(REMIND_SET, {"text": "call mum", "at": "17:00"})
    outcome = await tools.call(REMINDER_CANCEL, {"id": 1})
    assert outcome.ok
    assert "call mum" in outcome.text and "17:00" in outcome.text
    assert await tools.store.pending() == []


async def test_cancel_already_delivered(tools: ReminderTools, clock: Clock) -> None:
    await tools.call(REMIND_SET, {"text": "call mum", "in_minutes": 5})
    await tools.store.claim_due(clock.now + timedelta(minutes=5))
    outcome = await tools.call(REMINDER_CANCEL, {"id": 1})
    assert not outcome.ok
    assert "#1 was already delivered" in outcome.text


async def test_cancel_twice(tools: ReminderTools) -> None:
    await tools.call(REMIND_SET, {"text": "call mum", "in_minutes": 5})
    await tools.call(REMINDER_CANCEL, {"id": 1})
    outcome = await tools.call(REMINDER_CANCEL, {"id": "1"})
    assert not outcome.ok
    assert "already cancelled" in outcome.text


@pytest.mark.parametrize("value", [99, "#99"])
async def test_cancel_unknown(tools: ReminderTools, value: object) -> None:
    outcome = await tools.call(REMINDER_CANCEL, {"id": value})
    assert not outcome.ok
    assert outcome.text == "Error: there is no reminder #99."


@pytest.mark.parametrize("arguments", [{}, {"id": "soon"}, {"id": True}])
async def test_cancel_bad_id(tools: ReminderTools, arguments: dict[str, object]) -> None:
    outcome = await tools.call(REMINDER_CANCEL, arguments)
    assert not outcome.ok
    assert "reminder number" in outcome.text


async def test_bad_json(tools: ReminderTools) -> None:
    outcome = await tools.call(REMIND_SET, "{not json")
    assert not outcome.ok
    assert "not valid JSON" in outcome.text


async def test_offered_on_owner_turns_only(tools: ReminderTools) -> None:
    assert await tools.open(TurnInput("owner", "hi", 1)) is tools
    assert await tools.open(TurnInput("ask", "93%", 2, "disk-check")) is None
    assert await tools.open(TurnInput("reminder", "call mum", 3)) is None
    assert all(tools.handles(name) for name in NAMES)
    assert not tools.handles("alfred_web_fetch")
