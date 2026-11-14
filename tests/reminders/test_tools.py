from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.config import Users
from alfred.reminders.store import Reminders
from alfred.reminders.tools import (
    NAMES,
    REMIND_SET,
    REMINDER_CANCEL,
    REMINDERS_LIST,
    ReminderTools,
)
from alfred.tools.base import AttemptContext, TurnInput
from tests.helpers import ANA, DIOGO

LISBON = ZoneInfo("Europe/Lisbon")
USERS = Users((DIOGO, ANA))
AS_DIOGO = AttemptContext(TurnInput("chat", "x", 1, user=DIOGO))
AS_ANA = AttemptContext(TurnInput("chat", "x", 2, user=ANA))


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
    return ReminderTools(Reminders(sessions), LISBON, "09:00", USERS, now=clock)


def test_definitions(tools: ReminderTools) -> None:
    names = {d["function"]["name"] for d in tools.definitions}
    assert names == NAMES
    set_description = next(
        d["function"]["description"]
        for d in tools.definitions
        if d["function"]["name"] == REMIND_SET
    )
    # The reminder vs to-do split is repeated where the model chooses.
    assert "'task' or 'todo'" in set_description
    assert "to-do list" in set_description


async def test_relative_time(tools: ReminderTools) -> None:
    outcome = await tools.call(REMIND_SET, {"text": "check the oven", "in_minutes": 90}, AS_DIOGO)
    assert outcome.ok
    assert outcome.text.startswith("Reminder set for today at 18:17: check the oven. ")
    # No number and no countdown for the model to repeat.
    assert "#" not in outcome.text and "in 1 h" not in outcome.text
    assert "don't mention a reminder number" in outcome.text
    [row] = await tools.store.pending("diogo")
    assert row.due_at == datetime(2026, 11, 9, 18, 17, tzinfo=UTC)


async def test_arguments_as_json_text(tools: ReminderTools) -> None:
    outcome = await tools.call(
        REMIND_SET, json.dumps({"text": "call mum", "at": "17:00"}), AS_DIOGO
    )
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
    outcome = await tools.call(REMIND_SET, arguments, AS_DIOGO)
    assert not outcome.ok
    assert "exactly one" in outcome.text
    assert await tools.store.pending("diogo") == []


@pytest.mark.parametrize("text", ["", "   "])
async def test_empty_text(tools: ReminderTools, text: str) -> None:
    outcome = await tools.call(REMIND_SET, {"text": text, "in_minutes": 5}, AS_DIOGO)
    assert not outcome.ok
    assert outcome.text.startswith("Error:")
    assert await tools.store.pending("diogo") == []


async def test_in_the_past(tools: ReminderTools) -> None:
    outcome = await tools.call(REMIND_SET, {"text": "x", "at": "2026-11-08 10:00"}, AS_DIOGO)
    assert not outcome.ok
    assert "already passed" in outcome.text
    assert await tools.store.pending("diogo") == []


async def test_unreadable_time(tools: ReminderTools) -> None:
    outcome = await tools.call(REMIND_SET, {"text": "x", "at": "five pm"}, AS_DIOGO)
    assert not outcome.ok
    assert '"HH:MM"' in outcome.text and '"YYYY-MM-DD"' in outcome.text


# --- Listing reminders --------------------------------------------------------------


async def test_list_two_reminders(tools: ReminderTools) -> None:
    await tools.call(REMIND_SET, {"text": "check the oven", "in_minutes": 90}, AS_DIOGO)
    await tools.call(REMIND_SET, {"text": "call mum", "at": "17:00"}, AS_DIOGO)
    outcome = await tools.call(REMINDERS_LIST, {}, AS_DIOGO)
    assert outcome.ok
    assert outcome.text.splitlines()[1:] == [
        "#2 Monday 9 November 2026, 17:00 (in 13 min): call mum",
        "#1 Monday 9 November 2026, 18:17 (in 1 h 30 min): check the oven",
    ]


async def test_list_nothing_set(tools: ReminderTools) -> None:
    outcome = await tools.call(REMINDERS_LIST, None, AS_DIOGO)
    assert outcome.ok
    assert outcome.text == "There are no reminders set."


# --- Cancelling a reminder ----------------------------------------------------------


async def test_cancel_a_set_reminder(tools: ReminderTools) -> None:
    await tools.call(REMIND_SET, {"text": "call mum", "at": "17:00"}, AS_DIOGO)
    outcome = await tools.call(REMINDER_CANCEL, {"id": 1}, AS_DIOGO)
    assert outcome.ok
    assert "call mum" in outcome.text and "17:00" in outcome.text
    assert await tools.store.pending("diogo") == []


async def test_cancel_already_delivered(tools: ReminderTools, clock: Clock) -> None:
    await tools.call(REMIND_SET, {"text": "call mum", "in_minutes": 5}, AS_DIOGO)
    await tools.store.claim_due(clock.now + timedelta(minutes=5))
    outcome = await tools.call(REMINDER_CANCEL, {"id": 1}, AS_DIOGO)
    assert not outcome.ok
    assert "#1 was already delivered" in outcome.text


async def test_cancel_twice(tools: ReminderTools) -> None:
    await tools.call(REMIND_SET, {"text": "call mum", "in_minutes": 5}, AS_DIOGO)
    await tools.call(REMINDER_CANCEL, {"id": 1}, AS_DIOGO)
    outcome = await tools.call(REMINDER_CANCEL, {"id": "1"}, AS_DIOGO)
    assert not outcome.ok
    assert "already cancelled" in outcome.text


@pytest.mark.parametrize("value", [99, "#99"])
async def test_cancel_unknown(tools: ReminderTools, value: object) -> None:
    outcome = await tools.call(REMINDER_CANCEL, {"id": value}, AS_DIOGO)
    assert not outcome.ok
    assert outcome.text == "Error: there is no reminder #99."


@pytest.mark.parametrize("arguments", [{}, {"id": "soon"}, {"id": True}])
async def test_cancel_bad_id(tools: ReminderTools, arguments: dict[str, object]) -> None:
    outcome = await tools.call(REMINDER_CANCEL, arguments, AS_DIOGO)
    assert not outcome.ok
    assert "reminder number" in outcome.text


async def test_bad_json(tools: ReminderTools) -> None:
    outcome = await tools.call(REMIND_SET, "{not json", AS_DIOGO)
    assert not outcome.ok
    assert "not valid JSON" in outcome.text


async def test_offered_on_owner_turns_only(tools: ReminderTools) -> None:
    assert await tools.open(TurnInput("chat", "hi", 1, user=DIOGO)) is tools
    assert await tools.open(TurnInput("ask", "93%", 2, "disk-check", user=DIOGO)) is None
    assert await tools.open(TurnInput("reminder", "call mum", 3, user=DIOGO)) is None
    assert all(tools.handles(name) for name in NAMES)
    assert not tools.handles("alfred_web_fetch")


# --- for: reminding other users (change add-multiple-users) --------------------------


def test_for_lists_the_users(tools: ReminderTools) -> None:
    (remind_set,) = [d for d in tools.definitions if d["function"]["name"] == REMIND_SET]
    users = remind_set["function"]["parameters"]["properties"]["for"]
    assert users["type"] == "array" and users["items"]["enum"] == ["diogo", "ana"]


async def test_remind_another_user(tools: ReminderTools) -> None:
    outcome = await tools.call(
        REMIND_SET, {"text": "buy milk", "at": "18:00", "for": ["ana"]}, AS_DIOGO
    )
    assert outcome.ok
    assert outcome.text.startswith("Reminder set for today at 18:00, to remind ana: buy milk.")
    assert "#" not in outcome.text
    [row] = await tools.store.pending("ana")
    assert (row.created_by, row.recipients) == ("diogo", ("ana",))


async def test_remind_both(tools: ReminderTools) -> None:
    arguments = {"text": "dinner with my parents", "at": "20:00", "for": ["ana", "diogo", "ana"]}
    outcome = await tools.call(REMIND_SET, arguments, AS_ANA)
    assert outcome.ok and "to remind ana, diogo" in outcome.text
    [row] = await tools.store.pending("diogo")
    assert (row.created_by, row.recipients) == ("ana", ("ana", "diogo"))


async def test_for_defaults_to_the_turns_user(tools: ReminderTools) -> None:
    for value in (None, [], ""):
        arguments = {"text": "yoga", "in_minutes": 10}
        if value is not None:
            arguments["for"] = value
        outcome = await tools.call(REMIND_SET, arguments, AS_ANA)
        assert outcome.ok and "to remind" not in outcome.text
    assert all(r.only_for("ana") for r in await tools.store.pending("ana"))
    assert await tools.store.pending("diogo") == []


async def test_for_a_single_name(tools: ReminderTools) -> None:
    outcome = await tools.call(REMIND_SET, {"text": "x", "in_minutes": 5, "for": "Ana "}, AS_DIOGO)
    assert outcome.ok and "to remind ana" in outcome.text


async def test_unknown_user(tools: ReminderTools) -> None:
    outcome = await tools.call(
        REMIND_SET, {"text": "buy milk", "at": "18:00", "for": ["bob"]}, AS_DIOGO
    )
    assert not outcome.ok
    assert "'bob'" in outcome.text and "diogo, ana" in outcome.text
    assert await tools.store.pending("diogo") == []


@pytest.mark.parametrize("value", [[1], {"ana": True}, 7])
async def test_for_must_be_names(tools: ReminderTools, value: object) -> None:
    outcome = await tools.call(REMIND_SET, {"text": "x", "in_minutes": 5, "for": value}, AS_DIOGO)
    assert not outcome.ok and "for must be a list of user names" in outcome.text


async def test_list_shows_who_set_it_and_who_for(tools: ReminderTools) -> None:
    await tools.call(REMIND_SET, {"text": "buy milk", "at": "18:00", "for": ["ana"]}, AS_DIOGO)
    await tools.call(
        REMIND_SET, {"text": "dinner", "at": "20:00", "for": ["ana", "diogo"]}, AS_DIOGO
    )
    await tools.call(REMIND_SET, {"text": "yoga", "at": "21:00"}, AS_ANA)
    ana = (await tools.call(REMINDERS_LIST, {}, AS_ANA)).text.splitlines()
    assert ana[1].endswith("buy milk (set by diogo; for ana)")
    assert ana[2].endswith("dinner (set by diogo; for ana, diogo)")
    assert ana[3].endswith("yoga")
    diogo = (await tools.call(REMINDERS_LIST, {}, AS_DIOGO)).text.splitlines()
    assert len(diogo) == 3
    assert diogo[1].endswith("buy milk (for ana)")
    assert diogo[2].endswith("dinner (for ana, diogo)")


async def test_cancel_by_a_recipient(tools: ReminderTools) -> None:
    await tools.call(
        REMIND_SET, {"text": "dinner", "at": "20:00", "for": ["ana", "diogo"]}, AS_DIOGO
    )
    [row] = await tools.store.pending("ana")
    outcome = await tools.call(REMINDER_CANCEL, {"id": row.id}, AS_ANA)
    assert outcome.ok and "dinner" in outcome.text and "set by diogo" in outcome.text
    assert await tools.store.pending("diogo") == []


async def test_cancel_not_involved_is_unknown(tools: ReminderTools) -> None:
    await tools.call(REMIND_SET, {"text": "yoga", "at": "21:00"}, AS_ANA)
    [row] = await tools.store.pending("ana")
    outcome = await tools.call(REMINDER_CANCEL, {"id": row.id}, AS_DIOGO)
    assert not outcome.ok and outcome.text == f"Error: there is no reminder #{row.id}."
    assert [r.id for r in await tools.store.pending("ana")] == [row.id]
