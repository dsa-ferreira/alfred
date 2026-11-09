"""Switching brains with alfred_set_model in a turn."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.brains import SET_MODEL
from alfred.agent.prompts import NO_REPLY, brain_line
from alfred.agent.turn import TurnInput
from tests.agent.turn_helpers import (
    GEMMA,
    agent,
    owner_turn,
    set_model,
    tool_result,
    tool_rows,
    two_brains,
)
from tests.helpers import FakeModel, add_queue_item, text

# --- brains ----------------------------------------------------------------------


async def test_another_brain_chosen(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, brains=two_brains("bonsai")).run(await owner_turn(sessions, "hi"))
    assert model.models == ["bonsai:8b"]
    assert brain_line("bonsai", "bonsai:8b", ["gemma"]) in model.requests[0][0][0]["content"]


async def test_switch_applies_from_the_next_turn(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    brains = two_brains()
    model = FakeModel([set_model("bonsai"), text("Very good, sir."), text("bonsai here")])
    with caplog.at_level(logging.INFO):
        first = await agent(sessions, model, brains=brains).run(
            await owner_turn(sessions, "set your brain to bonsai")
        )
    assert first.reply == "Very good, sir." and first.fallback_notice is None
    assert "bonsai" in tool_result(model, 1) and "next message" in tool_result(model, 1)
    assert brains.current.name == "bonsai"
    await agent(sessions, model, brains=brains).run(await owner_turn(sessions, "hi"))
    assert model.models == [GEMMA, GEMMA, "bonsai:8b"]
    assert brain_line("bonsai", "bonsai:8b", ["gemma"]) in model.requests[2][0][0]["content"]
    assert any(
        "switched" in r.getMessage() and "gemma" in r.getMessage() and "bonsai" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO
    )
    [row] = await tool_rows(sessions)
    assert row.tool == SET_MODEL and row.arguments == {"model": "bonsai"} and row.ok


async def test_set_model_offered_with_the_brain_names(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, brains=two_brains()).run(await owner_turn(sessions, "hi"))
    [tool] = [t for t in model.requests[0][1] if t["function"]["name"] == SET_MODEL]
    parameters = tool["function"]["parameters"]
    assert parameters["properties"]["model"]["enum"] == ["gemma", "bonsai"]
    assert "brain" in tool["function"]["description"]


async def test_set_model_not_offered_with_one_brain(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model).run(await owner_turn(sessions, "hi"))
    assert SET_MODEL not in model.tool_names()


async def test_ask_turns_cant_switch(sessions: async_sessionmaker[AsyncSession]) -> None:
    item = await add_queue_item(sessions, "ask", "anything?", "disk-check")
    model = FakeModel([text(NO_REPLY)])
    await agent(sessions, model, brains=two_brains()).run(
        TurnInput("ask", "anything?", item, "disk-check")
    )
    assert SET_MODEL not in model.tool_names()


async def test_unknown_brain(sessions: async_sessionmaker[AsyncSession]) -> None:
    brains = two_brains()
    model = FakeModel([set_model("claude"), text("No such brain, sir.")])
    await agent(sessions, model, brains=brains).run(await owner_turn(sessions, "use claude"))
    result = tool_result(model, 1)
    assert result.startswith("Error:") and "gemma" in result and "bonsai" in result
    assert brains.current.name == "gemma"


async def test_brain_already_current(sessions: async_sessionmaker[AsyncSession]) -> None:
    brains = two_brains()
    model = FakeModel([set_model("gemma"), text("Already so, sir.")])
    await agent(sessions, model, brains=brains).run(await owner_turn(sessions, "use gemma"))
    assert "already in use" in tool_result(model, 1)
    assert brains.current.name == "gemma"
