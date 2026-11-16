"""The user's facts in whole turns (change add-user-facts, design D8, D9)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.memory.facts import _FORGET, _REMEMBER, FactStore, FactTools
from alfred.tools.base import AskTurn, TurnInput
from tests.agent.turn_helpers import agent, owner_turn, reminder_turn
from tests.helpers import (
    ANA,
    DIOGO,
    FakeEmbedder,
    FakeModel,
    add_queue_item,
    calls,
    make_config,
    text,
)

SHELLFISH = "[1] allergic to shellfish (told)"


def tools(sessions: async_sessionmaker[AsyncSession], **memory: object) -> FactTools:
    cfg = make_config(memory=memory)  # type: ignore[arg-type]
    return FactTools(FactStore(cfg, sessions, FakeEmbedder(default=[1.0, 0.0])), cfg.memory)


def system(model: FakeModel, request: int = 0) -> str:
    return model.requests[request][0][0]["content"]


async def ask_turn(sessions: async_sessionmaker[AsyncSession]) -> TurnInput:
    item = await add_queue_item(sessions, "ask", "anything?", "disk-check")
    return AskTurn("anything?", item, "disk-check", user=DIOGO)


async def run(
    sessions: async_sessionmaker[AsyncSession], facts: FactTools, turn: TurnInput, **memory: Any
) -> FakeModel:
    model = FakeModel([text("ok")])
    await agent(sessions, model, facts=facts, memory_config=memory).run(turn)
    return model


async def test_facts_last_in_every_kind_of_turn(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = tools(sessions, facts_in_context=True)
    await facts.facts.add("diogo", "allergic to shellfish", "told")
    for turn in (
        await owner_turn(sessions, "a birthday dinner restaurant?"),
        await ask_turn(sessions),
        await reminder_turn(sessions),
    ):
        prompt = system(await run(sessions, facts, turn, facts_in_context=True))
        assert prompt.endswith(SHELLFISH), turn.kind
        assert "What you know about diogo (all 1 fact)." in prompt


async def test_flag_off_no_facts_anywhere(sessions: async_sessionmaker[AsyncSession]) -> None:
    facts = tools(sessions)
    await facts.facts.add("diogo", "allergic to shellfish", "told")
    for turn in (
        await owner_turn(sessions, "hi"),
        await ask_turn(sessions),
        await reminder_turn(sessions),
    ):
        prompt = system(await run(sessions, facts, turn))
        assert "shellfish" not in prompt and "What you know about" not in prompt


async def test_another_users_facts_never_shown(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = tools(sessions, facts_in_context=True)
    await facts.facts.add("diogo", "allergic to shellfish", "told")
    model = await run(sessions, facts, await owner_turn(sessions, "hi", ANA), facts_in_context=True)
    assert "shellfish" not in str(model.requests[0])
    assert "What you know about" not in system(model)


async def test_block_is_the_same_while_facts_are(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = tools(sessions, facts_in_context=True)
    await facts.facts.add("diogo", "allergic to shellfish", "told")
    await facts.facts.add("diogo", "runs a homelab", "learned")
    first = system(
        await run(sessions, facts, await owner_turn(sessions, "one"), facts_in_context=True)
    )
    second = system(
        await run(sessions, facts, await owner_turn(sessions, "two"), facts_in_context=True)
    )
    block = first[first.index("What you know about") :]
    assert second.endswith(block)


async def test_remember_and_forget_in_a_turn(sessions: async_sessionmaker[AsyncSession]) -> None:
    facts = tools(sessions, facts_in_context=True)
    model = FakeModel(
        [
            calls((_REMEMBER, '{"text": "is vegetarian"}')),
            calls((_FORGET, '{"ids": [1]}')),
            text("Done, sir."),
        ]
    )
    turn = await owner_turn(sessions, "remember I'm vegetarian, then forget it")
    await agent(sessions, model, facts=facts, memory_config={"facts_in_context": True}).run(turn)
    assert {_REMEMBER, _FORGET} <= model.tool_names()
    assert model.requests[1][0][-1]["content"] == "Saved as fact 1."
    assert model.requests[2][0][-1]["content"] == "Forgot 1."
    assert await facts.facts.list("diogo") == []


async def test_no_fact_tools_on_ask_turns(sessions: async_sessionmaker[AsyncSession]) -> None:
    facts = tools(sessions, facts_in_context=True)
    model = await run(sessions, facts, await ask_turn(sessions), facts_in_context=True)
    assert not {_REMEMBER, _FORGET} & model.tool_names()


async def test_remembering_a_near_duplicate_makes_no_extra_request(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The duplicate check is left to the background (change fix-fact-removals, D6)."""
    facts = tools(sessions, facts_in_context=True)
    await facts.facts.add("diogo", "allergic to shellfish", "told")
    model = FakeModel([calls((_REMEMBER, '{"text": "is allergic to shellfish"}')), text("Noted.")])
    turn = await owner_turn(sessions, "remember I'm allergic to shellfish")
    await agent(sessions, model, facts=facts, memory_config={"facts_in_context": True}).run(turn)
    assert len(model.requests) == 2  # the tool call and the answer, nothing else
    assert model.requests[1][0][-1]["content"] == "Saved as fact 2."
    assert len(await facts.facts.list("diogo")) == 2
