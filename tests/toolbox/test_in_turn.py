"""``alfred_calculate`` and ``alfred_date`` in whole turns (change add-utility-tools)."""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.kernel.turn import AskTurn
from alfred.toolbox.calculator import _CALCULATE
from alfred.toolbox.dates import _DATE
from tests.agent.turn_helpers import agent, owner_turn, tool_result, tool_rows
from tests.helpers import DIOGO, FakeModel, add_queue_item, calls, text


async def test_calculate_then_date_then_answer(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel(
        [
            calls((_CALCULATE, json.dumps({"expression": "347 * 28.5"}))),
            calls((_DATE, json.dumps({"op": "between", "from": "today", "to": "2026-12-25"}))),
            text("9889.5, sir, and Christmas is 53 days away."),
        ]
    )
    result = await agent(sessions, model).run(
        await owner_turn(sessions, "347 times 28.5? and how long until Christmas?")
    )
    assert result.reply == "9889.5, sir, and Christmas is 53 days away."
    assert tool_result(model, 1) == "347 * 28.5 = 9889.5"
    # The agent's clock is Monday 2 November 2026, 14:05 in Lisbon.
    assert tool_result(model, 2) == (
        "Friday 25 December 2026 is 53 days (7 weeks and 4 days) after Monday 2 November 2026."
    )
    rows = await tool_rows(sessions)
    assert [(r.tool, r.ok) for r in rows] == [(_CALCULATE, True), (_DATE, True)]


async def test_errors_reach_the_model(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel(
        [
            calls(
                (_CALCULATE, json.dumps({"expression": "1", "from_unit": "cup", "to_unit": "g"})),
                (_DATE, json.dumps({"op": "now_in", "zone": "Tokyo"})),
            ),
            text("I'm afraid I need the flour's density, sir."),
        ]
    )
    await agent(sessions, model).run(await owner_turn(sessions, "a cup of flour in grams?"))
    cup, zone = model.requests[1][0][-2:]
    assert "such as the ingredient's density" in cup["content"]
    assert "use an IANA name such as Asia/Tokyo" in zone["content"]
    assert [r.ok for r in await tool_rows(sessions)] == [False, False]


async def test_ask_turn_can_calculate(sessions: async_sessionmaker[AsyncSession]) -> None:
    item = await add_queue_item(sessions, "ask", "used 930 of 1000 GB", "disk-check")
    model = FakeModel(
        [
            calls((_CALCULATE, json.dumps({"expression": "930 / 1000 * 100"}))),
            text("The data disk is 93% full, sir."),
        ]
    )
    result = await agent(sessions, model).run(
        AskTurn("used 930 of 1000 GB", item, "disk-check", user=DIOGO)
    )
    assert result.reply == "The data disk is 93% full, sir."
    assert tool_result(model, 1) == "930 / 1000 * 100 = 93"
