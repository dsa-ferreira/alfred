from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.kernel.toolset import AttemptContext
from alfred.kernel.turn import AskTurn, ChatTurn, ReminderTurn
from alfred.kernel.users import Users
from alfred.reminders.store import Reminders
from alfred.reminders.tools import (
    _REMIND_SET,
    _REMINDER_CANCEL,
    _REMINDERS_LIST,
    ReminderTools,
    _parse_text,
)
from alfred.times import WhenError
from tests.helpers import ANA, DIOGO

LISBON = ZoneInfo("Europe/Lisbon")
USERS = Users((DIOGO, ANA))
AS_DIOGO = AttemptContext(ChatTurn("x", 1, user=DIOGO))
AS_ANA = AttemptContext(ChatTurn("x", 2, user=ANA))


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
    return ReminderTools(Reminders(sessions, LISBON), LISBON, "09:00", USERS, now=clock)


def test_definitions(tools: ReminderTools) -> None:
    names = {d["function"]["name"] for d in tools.definitions}
    assert names == ReminderTools.names
    set_description = next(
        d["function"]["description"]
        for d in tools.definitions
        if d["function"]["name"] == _REMIND_SET
    )
    # The reminder vs to-do split is repeated where the model chooses.
    assert "'task' or 'todo'" in set_description
    assert "to-do list" in set_description


async def test_relative_time(tools: ReminderTools) -> None:
    outcome = await tools.call(_REMIND_SET, {"text": "check the oven", "in_minutes": 90}, AS_DIOGO)
    assert outcome.ok
    assert outcome.text.startswith("Reminder set for today at 18:17: check the oven. ")
    # No number and no countdown for the model to repeat.
    assert "#" not in outcome.text and "in 1 h" not in outcome.text
    assert "don't mention a reminder number" in outcome.text
    [row] = await tools.store.pending("diogo")
    assert row.due_at == datetime(2026, 11, 9, 18, 17, tzinfo=UTC)


async def test_arguments_as_json_text(tools: ReminderTools) -> None:
    outcome = await tools.call(
        _REMIND_SET, json.dumps({"text": "call mum", "at": "17:00"}), AS_DIOGO
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
    outcome = await tools.call(_REMIND_SET, arguments, AS_DIOGO)
    assert not outcome.ok
    assert "exactly one" in outcome.text
    assert await tools.store.pending("diogo") == []


@pytest.mark.parametrize("text", ["", "   "])
async def test_empty_text(tools: ReminderTools, text: str) -> None:
    outcome = await tools.call(_REMIND_SET, {"text": text, "in_minutes": 5}, AS_DIOGO)
    assert not outcome.ok
    assert outcome.text.startswith("Error:")
    assert await tools.store.pending("diogo") == []


async def test_in_the_past(tools: ReminderTools) -> None:
    outcome = await tools.call(_REMIND_SET, {"text": "x", "at": "2026-11-08 10:00"}, AS_DIOGO)
    assert not outcome.ok
    assert "already passed" in outcome.text
    assert await tools.store.pending("diogo") == []


async def test_unreadable_time(tools: ReminderTools) -> None:
    outcome = await tools.call(_REMIND_SET, {"text": "x", "at": "five pm"}, AS_DIOGO)
    assert not outcome.ok
    assert '"HH:MM"' in outcome.text and '"YYYY-MM-DD"' in outcome.text


# --- Listing reminders --------------------------------------------------------------


async def test_list_two_reminders(tools: ReminderTools) -> None:
    await tools.call(_REMIND_SET, {"text": "check the oven", "in_minutes": 90}, AS_DIOGO)
    await tools.call(_REMIND_SET, {"text": "call mum", "at": "17:00"}, AS_DIOGO)
    outcome = await tools.call(_REMINDERS_LIST, {}, AS_DIOGO)
    assert outcome.ok
    assert outcome.text.splitlines()[1:] == [
        "#2 Monday 9 November 2026, 17:00 (in 13 min): call mum",
        "#1 Monday 9 November 2026, 18:17 (in 1 h 30 min): check the oven",
    ]


async def test_list_nothing_set(tools: ReminderTools) -> None:
    outcome = await tools.call(_REMINDERS_LIST, None, AS_DIOGO)
    assert outcome.ok
    assert outcome.text == "There are no reminders set."


# --- Cancelling a reminder ----------------------------------------------------------


async def test_cancel_a_set_reminder(tools: ReminderTools) -> None:
    await tools.call(_REMIND_SET, {"text": "call mum", "at": "17:00"}, AS_DIOGO)
    outcome = await tools.call(_REMINDER_CANCEL, {"id": 1}, AS_DIOGO)
    assert outcome.ok
    assert "call mum" in outcome.text and "17:00" in outcome.text
    assert await tools.store.pending("diogo") == []


async def test_cancel_already_delivered(tools: ReminderTools, clock: Clock) -> None:
    await tools.call(_REMIND_SET, {"text": "call mum", "in_minutes": 5}, AS_DIOGO)
    await tools.store.claim_due(clock.now + timedelta(minutes=5))
    outcome = await tools.call(_REMINDER_CANCEL, {"id": 1}, AS_DIOGO)
    assert not outcome.ok
    assert "#1 was already delivered" in outcome.text


async def test_cancel_twice(tools: ReminderTools) -> None:
    await tools.call(_REMIND_SET, {"text": "call mum", "in_minutes": 5}, AS_DIOGO)
    await tools.call(_REMINDER_CANCEL, {"id": 1}, AS_DIOGO)
    outcome = await tools.call(_REMINDER_CANCEL, {"id": "1"}, AS_DIOGO)
    assert not outcome.ok
    assert "already cancelled" in outcome.text


@pytest.mark.parametrize("value", [99, "#99"])
async def test_cancel_unknown(tools: ReminderTools, value: object) -> None:
    outcome = await tools.call(_REMINDER_CANCEL, {"id": value}, AS_DIOGO)
    assert not outcome.ok
    assert outcome.text == "Error: there is no reminder #99."


@pytest.mark.parametrize("arguments", [{}, {"id": "soon"}, {"id": True}])
async def test_cancel_bad_id(tools: ReminderTools, arguments: dict[str, object]) -> None:
    outcome = await tools.call(_REMINDER_CANCEL, arguments, AS_DIOGO)
    assert not outcome.ok
    assert "reminder number" in outcome.text


async def test_bad_json(tools: ReminderTools) -> None:
    outcome = await tools.call(_REMIND_SET, "{not json", AS_DIOGO)
    assert not outcome.ok
    assert "not valid JSON" in outcome.text


def _names(toolset: object) -> set[str]:
    return {d["function"]["name"] for d in toolset.definitions}  # type: ignore[attr-defined]


async def test_offered_by_turn_kind(tools: ReminderTools) -> None:
    assert await tools.open(ChatTurn("hi", 1, user=DIOGO)) is tools
    assert await tools.open(AskTurn("93%", 2, "disk-check", user=DIOGO)) is None
    reminder_turn = await tools.open(ReminderTurn("brief me", 3, user=DIOGO))
    assert reminder_turn is not None and _names(reminder_turn) == {_REMINDERS_LIST}
    assert reminder_turn.handles(_REMINDERS_LIST)
    assert not reminder_turn.handles(_REMIND_SET) and not reminder_turn.handles(_REMINDER_CANCEL)
    assert all(tools.handles(name) for name in ReminderTools.names)
    assert not tools.handles("alfred_web_fetch")


async def test_reminder_turn_lists_the_recipients_reminders(tools: ReminderTools) -> None:
    await tools.call(_REMIND_SET, {"text": "buy milk", "at": "18:00", "for": ["ana"]}, AS_DIOGO)
    await tools.call(_REMIND_SET, {"text": "gym", "at": "19:00"}, AS_DIOGO)
    turn = ReminderTurn("brief me", 3, user=ANA)
    reminder_turn = await tools.open(turn)
    assert reminder_turn is not None
    outcome = await reminder_turn.call(_REMINDERS_LIST, {}, AttemptContext(turn))
    assert outcome.ok and "buy milk" in outcome.text and "gym" not in outcome.text
    refused = await reminder_turn.call(_REMIND_SET, {"text": "x"}, AttemptContext(turn))
    assert not refused.ok


async def test_reminder_turn_respects_the_allow_list(tools: ReminderTools) -> None:
    from alfred.kernel.access import AllowList, Gated

    reminder_turn = await tools.open(ReminderTurn("brief me", 3, user=DIOGO))
    assert reminder_turn is not None
    gated = Gated(reminder_turn, AllowList.of("alfred_remind_set", "alfred_web_*"))
    assert gated.definitions == [] and not gated.handles(_REMINDERS_LIST)


# --- for: reminding other users (change add-multiple-users) --------------------------


def test_for_lists_the_users(tools: ReminderTools) -> None:
    (remind_set,) = [d for d in tools.definitions if d["function"]["name"] == _REMIND_SET]
    users = remind_set["function"]["parameters"]["properties"]["for"]
    assert users["type"] == "array" and users["items"]["enum"] == ["diogo", "ana"]


async def test_remind_another_user(tools: ReminderTools) -> None:
    outcome = await tools.call(
        _REMIND_SET, {"text": "buy milk", "at": "18:00", "for": ["ana"]}, AS_DIOGO
    )
    assert outcome.ok
    assert outcome.text.startswith("Reminder set for today at 18:00, to remind ana: buy milk.")
    assert "#" not in outcome.text
    [row] = await tools.store.pending("ana")
    assert (row.created_by, row.recipients) == ("diogo", ("ana",))


async def test_remind_both(tools: ReminderTools) -> None:
    arguments = {"text": "dinner with my parents", "at": "20:00", "for": ["ana", "diogo", "ana"]}
    outcome = await tools.call(_REMIND_SET, arguments, AS_ANA)
    assert outcome.ok and "to remind ana, diogo" in outcome.text
    [row] = await tools.store.pending("diogo")
    assert (row.created_by, row.recipients) == ("ana", ("ana", "diogo"))


async def test_for_defaults_to_the_turns_user(tools: ReminderTools) -> None:
    for value in (None, [], ""):
        arguments = {"text": "yoga", "in_minutes": 10}
        if value is not None:
            arguments["for"] = value
        outcome = await tools.call(_REMIND_SET, arguments, AS_ANA)
        assert outcome.ok and "to remind" not in outcome.text
    assert all(r.only_for("ana") for r in await tools.store.pending("ana"))
    assert await tools.store.pending("diogo") == []


async def test_for_a_single_name(tools: ReminderTools) -> None:
    outcome = await tools.call(_REMIND_SET, {"text": "x", "in_minutes": 5, "for": "Ana "}, AS_DIOGO)
    assert outcome.ok and "to remind ana" in outcome.text


async def test_unknown_user(tools: ReminderTools) -> None:
    outcome = await tools.call(
        _REMIND_SET, {"text": "buy milk", "at": "18:00", "for": ["bob"]}, AS_DIOGO
    )
    assert not outcome.ok
    assert "'bob'" in outcome.text and "diogo, ana" in outcome.text
    assert await tools.store.pending("diogo") == []


@pytest.mark.parametrize("value", [[1], {"ana": True}, 7])
async def test_for_must_be_names(tools: ReminderTools, value: object) -> None:
    outcome = await tools.call(_REMIND_SET, {"text": "x", "in_minutes": 5, "for": value}, AS_DIOGO)
    assert not outcome.ok and "for must be a list of user names" in outcome.text


async def test_list_shows_who_set_it_and_who_for(tools: ReminderTools) -> None:
    await tools.call(_REMIND_SET, {"text": "buy milk", "at": "18:00", "for": ["ana"]}, AS_DIOGO)
    await tools.call(
        _REMIND_SET, {"text": "dinner", "at": "20:00", "for": ["ana", "diogo"]}, AS_DIOGO
    )
    await tools.call(_REMIND_SET, {"text": "yoga", "at": "21:00"}, AS_ANA)
    ana = (await tools.call(_REMINDERS_LIST, {}, AS_ANA)).text.splitlines()
    assert ana[1].endswith("buy milk (set by diogo; for ana)")
    assert ana[2].endswith("dinner (set by diogo; for ana, diogo)")
    assert ana[3].endswith("yoga")
    diogo = (await tools.call(_REMINDERS_LIST, {}, AS_DIOGO)).text.splitlines()
    assert len(diogo) == 3
    assert diogo[1].endswith("buy milk (for ana)")
    assert diogo[2].endswith("dinner (for ana, diogo)")


async def test_cancel_by_a_recipient(tools: ReminderTools) -> None:
    await tools.call(
        _REMIND_SET, {"text": "dinner", "at": "20:00", "for": ["ana", "diogo"]}, AS_DIOGO
    )
    [row] = await tools.store.pending("ana")
    outcome = await tools.call(_REMINDER_CANCEL, {"id": row.id}, AS_ANA)
    assert outcome.ok and "dinner" in outcome.text and "20:00" in outcome.text
    assert outcome.text.endswith("it stays set for diogo.")
    assert await tools.store.pending("ana") == []
    assert [r.id for r in await tools.store.pending("diogo")] == [row.id]


async def test_cancel_by_the_last_recipient(tools: ReminderTools) -> None:
    await tools.call(_REMIND_SET, {"text": "buy milk", "at": "18:00", "for": ["ana"]}, AS_DIOGO)
    [row] = await tools.store.pending("ana")
    outcome = await tools.call(_REMINDER_CANCEL, {"id": row.id}, AS_ANA)
    assert outcome.ok and outcome.text.startswith(f"Reminder #{row.id} cancelled")
    assert await tools.store.pending("diogo") == []


async def test_cancel_not_involved_is_unknown(tools: ReminderTools) -> None:
    await tools.call(_REMIND_SET, {"text": "yoga", "at": "21:00"}, AS_ANA)
    [row] = await tools.store.pending("ana")
    outcome = await tools.call(_REMINDER_CANCEL, {"id": row.id}, AS_DIOGO)
    assert not outcome.ok and outcome.text == f"Error: there is no reminder #{row.id}."
    assert [r.id for r in await tools.store.pending("ana")] == [row.id]


# --- Repeating and quiet reminders (change add-recurring-reminders) --------------------


def test_repeat_parameters(tools: ReminderTools) -> None:
    (remind_set,) = [d for d in tools.definitions if d["function"]["name"] == _REMIND_SET]
    function = remind_set["function"]
    properties = function["parameters"]["properties"]
    assert properties["repeat"]["enum"] == ["daily", "weekdays", "weekends", "weekly"]
    assert properties["days"]["items"]["enum"][0] == "mon"
    assert properties["quiet_ok"]["type"] == "boolean"
    assert "brief me on the weather" in function["description"]
    assert "not done now" in function["description"]


async def test_set_a_series(tools: ReminderTools, clock: Clock) -> None:
    clock.now = datetime(2026, 11, 15, 14, 47, tzinfo=LISBON)
    outcome = await tools.call(
        _REMIND_SET,
        {"text": "brief me on the weather", "repeat": "weekdays", "at": "08:00"},
        AS_DIOGO,
    )
    assert outcome.ok
    assert outcome.text.startswith(
        "Reminder set every weekday at 08:00, starting tomorrow at 08:00: brief me on the weather."
    )
    assert "#" not in outcome.text
    [row] = await tools.store.pending("diogo")
    assert row.due_at == datetime(2026, 11, 16, 8, 0, tzinfo=LISBON)
    assert row.rule is not None and row.rule.repeat == "weekdays" and not row.quiet_ok


async def test_set_a_series_for_others(tools: ReminderTools, clock: Clock) -> None:
    clock.now = datetime(2026, 11, 17, 21, 0, tzinfo=LISBON)
    outcome = await tools.call(
        _REMIND_SET,
        {
            "text": "take the bins out",
            "repeat": "weekly",
            "days": ["tue", "fri"],
            "at": "20:00",
            "for": ["ana"],
        },
        AS_DIOGO,
    )
    assert outcome.ok
    assert outcome.text.startswith(
        "Reminder set every Tuesday and Friday at 20:00, starting on Friday at 20:00, "
        "to remind ana: take the bins out."
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"text": "x", "repeat": "monthly", "at": "08:00"},
        {"text": "x", "repeat": "weekly", "at": "08:00"},
        {"text": "x", "repeat": "daily", "at": "2026-11-20 08:00"},
        {"text": "x", "repeat": "daily", "in_minutes": 60},
        {"text": "x", "days": ["tue"], "at": "08:00"},
        {"text": "x", "at": "08:00", "quiet_ok": "maybe"},
    ],
)
async def test_series_errors_store_nothing(
    tools: ReminderTools, arguments: dict[str, object]
) -> None:
    outcome = await tools.call(_REMIND_SET, arguments, AS_DIOGO)
    assert not outcome.ok and outcome.text.startswith("Error:")
    assert await tools.store.pending("diogo") == []


async def test_list_a_quiet_series(tools: ReminderTools, clock: Clock) -> None:
    clock.now = datetime(2026, 11, 15, 20, 0, tzinfo=LISBON)
    await tools.call(
        _REMIND_SET,
        {"text": "brief me", "repeat": "weekdays", "at": "08:00", "quiet_ok": True},
        AS_DIOGO,
    )
    await tools.call(
        _REMIND_SET, {"text": "tell me if Dune is done", "at": "18:00", "quiet_ok": True}, AS_DIOGO
    )
    outcome = await tools.call(_REMINDERS_LIST, {}, AS_DIOGO)
    assert outcome.text.splitlines()[1:] == [
        "#1 Monday 16 November 2026, 08:00 (in 12 h), every weekday at 08:00, may stay quiet: "
        "brief me",
        "#2 Monday 16 November 2026, 18:00 (in 22 h), may stay quiet: tell me if Dune is done",
    ]


async def test_list_a_series_after_it_fired(tools: ReminderTools, clock: Clock) -> None:
    clock.now = datetime(2026, 11, 15, 20, 0, tzinfo=LISBON)
    await tools.call(
        _REMIND_SET, {"text": "brief me", "repeat": "weekdays", "at": "08:00"}, AS_DIOGO
    )
    await tools.store.claim_due(datetime(2026, 11, 16, 8, 0, tzinfo=LISBON))
    clock.now = datetime(2026, 11, 16, 9, 0, tzinfo=LISBON)
    lines = (await tools.call(_REMINDERS_LIST, {}, AS_DIOGO)).text.splitlines()[1:]
    assert lines == [
        "#1 Tuesday 17 November 2026, 08:00 (in 23 h), every weekday at 08:00: brief me"
    ]


async def test_cancel_a_series(tools: ReminderTools, clock: Clock) -> None:
    clock.now = datetime(2026, 11, 15, 20, 0, tzinfo=LISBON)
    await tools.call(
        _REMIND_SET, {"text": "brief me", "repeat": "weekdays", "at": "08:00"}, AS_DIOGO
    )
    outcome = await tools.call(_REMINDER_CANCEL, {"id": 1}, AS_DIOGO)
    assert outcome.ok
    assert "every weekday at 08:00, next Monday 16 November 2026, 08:00" in outcome.text
    assert await tools.store.pending("diogo") == []


# --- Reminder text ------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", None, 12])
def test_parse_empty_text(value: object) -> None:
    with pytest.raises(WhenError, match="text"):
        _parse_text(value)


def test_text_is_stripped_and_capped() -> None:
    assert _parse_text("  call mum ") == "call mum"
    with pytest.raises(WhenError, match="too long"):
        _parse_text("x" * 501)
