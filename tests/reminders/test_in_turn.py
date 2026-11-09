"""Reminder tools and reminder turns (change add-reminders)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.conversation import END_CONVERSATION
from alfred.agent.model import ModelUnreachable
from alfred.agent.prompts import ASK_RULE, NO_REPLY, REMINDER_RULE
from alfred.agent.turn import TurnFailed, TurnInput
from alfred.history import History
from alfred.reminders.store import Reminders
from alfred.reminders.tools import NAMES as REMINDER_TOOLS
from alfred.reminders.tools import REMIND_SET, ReminderTools
from tests.agent.turn_helpers import (
    DUE,
    Clock,
    agent,
    owner_turn,
    reminder_tools,
    reminder_turn,
    tool_rows,
)
from tests.helpers import FakeModel, add_queue_item, calls, text

# --- reminders -----------------------------------------------------------------


async def test_reminder_tools_offered_in_owner_turns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock(DUE)
    model = FakeModel([text("ok")])
    a = agent(sessions, model, reminders=reminder_tools(sessions, clock), now=clock)
    await a.run(await owner_turn(sessions, "hi"))
    assert model.tool_names() >= REMINDER_TOOLS


async def test_reminder_tools_not_offered_in_ask_turns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock(DUE)
    model = FakeModel([text(NO_REPLY), calls((REMIND_SET, '{"text": "x", "in_minutes": 5}'))])
    model.script.append(text("done"))
    a = agent(sessions, model, reminders=reminder_tools(sessions, clock), now=clock)
    item = await add_queue_item(sessions, "ask", "93%", "disk-check")
    await a.run(TurnInput("ask", "93%", item, "disk-check"))
    assert not REMINDER_TOOLS & model.tool_names(0)
    # Called anyway (from a reminder turn): refused, nothing stored.
    await a.run(await reminder_turn(sessions))
    assert not REMINDER_TOOLS & model.tool_names(1)
    assert "unknown tool" in model.requests[2][0][-1]["content"].lower()
    assert await Reminders(sessions).pending() == []


async def test_owner_sets_a_reminder(sessions: async_sessionmaker[AsyncSession]) -> None:
    clock = Clock(datetime(2026, 11, 9, 16, 47, tzinfo=UTC))
    model = FakeModel(
        [
            calls((REMIND_SET, '{"text": "check the oven", "in_minutes": 90}')),
            text("Very good. 18:17."),
        ]
    )
    a = agent(sessions, model, reminders=reminder_tools(sessions, clock), now=clock)
    result = await a.run(await owner_turn(sessions, "remind me in 90 minutes to check the oven"))
    assert result.reply == "Very good. 18:17."
    assert model.requests[1][0][-1]["content"].startswith(
        "Reminder set for today at 18:17: check the oven."
    )
    [row] = await tool_rows(sessions)
    assert (row.tool, row.ok) == (REMIND_SET, True)


async def test_reminder_turn_on_time(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    await history.append("owner", "earlier")
    await history.append("assistant", "earlier reply")
    model = FakeModel([text("Your call to mum is due, sir.")])
    a = agent(sessions, model, now=DUE)
    result = await a.run(await reminder_turn(sessions))
    assert result.reply == "Your call to mum is due, sir."
    messages = model.requests[0][0]
    system = messages[0]["content"]
    assert system.endswith(REMINDER_RULE)
    assert ASK_RULE not in system and NO_REPLY not in system
    assert [m["content"] for m in messages[1:]] == [
        "earlier",
        "earlier reply",
        "[reminder:#12 due Monday 9 November 2026, 17:00; now 17:00] call mum",
    ]


async def test_reminder_turn_gets_the_ask_tool_set(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock(DUE)
    owner_model = FakeModel([text("ok")])
    reminder_model = FakeModel([text("ok")])
    ask_model = FakeModel([text("ok")])
    tools = reminder_tools(sessions, clock)
    await agent(sessions, owner_model, reminders=tools, now=clock).run(
        await owner_turn(sessions, "hi")
    )
    await agent(sessions, reminder_model, reminders=tools, now=clock).run(
        await reminder_turn(sessions)
    )
    item = await add_queue_item(sessions, "ask", "93%", "disk-check")
    await agent(sessions, ask_model, reminders=tools, now=clock).run(
        TurnInput("ask", "93%", item, "disk-check")
    )
    assert reminder_model.tool_names() == ask_model.tool_names()
    assert END_CONVERSATION not in reminder_model.tool_names()
    assert reminder_model.tool_names() < owner_model.tool_names()


async def test_reminder_turn_late_retry_shows_the_new_now(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock(DUE + timedelta(minutes=12))
    model = FakeModel([ModelUnreachable("ai.lan asleep"), text("Rather late, sir: call mum.")])
    a = agent(sessions, model, now=clock)
    turn = await reminder_turn(sessions)
    with pytest.raises(TurnFailed) as failed:
        await a.run(turn)
    assert failed.value.retryable
    assert model.requests[0][0][-1]["content"].startswith(
        "[reminder:#12 due Monday 9 November 2026, 17:00; now 17:12]"
    )
    clock.now = DUE + timedelta(hours=2, minutes=40)
    await a.run(turn)
    assert model.requests[1][0][-1]["content"] == (
        "[reminder:#12 due Monday 9 November 2026, 17:00; now 19:40] call mum"
    )


async def test_reminder_turn_days_late_shows_the_date(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, now=DUE + timedelta(days=3)).run(await reminder_turn(sessions))
    assert model.requests[0][0][-1]["content"] == (
        "[reminder:#12 due Monday 9 November 2026, 17:00; now Thursday 12 November 2026, 17:00]"
        " call mum"
    )


async def test_reminder_tool_failure_reaches_the_model(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    class Broken(ReminderTools):
        def __init__(self) -> None:
            pass

        async def call(self, name: str, raw_arguments: object, attempt: object = None) -> object:
            raise OSError("database down")

    model = FakeModel([calls((REMIND_SET, '{"text": "x", "in_minutes": 5}')), text("Sorry.")])
    a = agent(sessions, model, reminders=Broken())  # type: ignore[arg-type]
    await a.run(await owner_turn(sessions, "remind me in 5 minutes"))
    assert "failed: OSError" in model.requests[1][0][-1]["content"]
