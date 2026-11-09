"""Switching brains with alfred_set_model in a turn."""

from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.brains import Brains
from alfred.agent.prompts import NO_REPLY, brain_line
from alfred.tools.base import AttemptContext, ToolOutcome, TurnInput
from alfred.tools.brains import SET_MODEL, BrainTools
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


# --- alfred_set_model ---------------------------------------------------------------

OWNER = TurnInput("owner", "use bonsai", 1)
TWO = {"gemma": "gemma4", "bonsai": "bonsai:8b"}


async def test_set_model_offered_on_owner_turns_with_two_brains() -> None:
    tools = BrainTools(Brains("gemma4", TWO))
    assert await tools.open(OWNER) is tools
    assert await tools.open(TurnInput("ask", "x", 2, "svc")) is None
    assert await BrainTools(Brains("gemma4")).open(OWNER) is None
    [definition] = tools.definitions
    assert definition["function"]["name"] == SET_MODEL
    assert definition["function"]["parameters"]["properties"]["model"]["enum"] == [
        "gemma",
        "bonsai",
    ]


async def test_set_model_switches() -> None:
    brains = Brains("gemma4", TWO)
    outcome = await BrainTools(brains).call(
        SET_MODEL, json.dumps({"model": "bonsai"}), AttemptContext(OWNER)
    )
    assert outcome.ok and brains.current.name == "bonsai"
    assert outcome.text.startswith('Switched to the brain "bonsai" (bonsai:8b).')


async def test_set_model_unknown_and_already() -> None:
    brains = Brains("gemma4", TWO)
    tools = BrainTools(brains)
    ctx = AttemptContext(OWNER)
    assert await tools.call(SET_MODEL, '{"model": "x"}', ctx) == ToolOutcome(
        False, "Error: unknown brain 'x'. The brains are: gemma, bonsai."
    )
    assert await tools.call(SET_MODEL, '{"model": "gemma"}', ctx) == ToolOutcome(
        True, 'The brain "gemma" is already in use; nothing changed.'
    )
    assert brains.current.name == "gemma"
