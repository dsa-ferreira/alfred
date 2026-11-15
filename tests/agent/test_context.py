"""Turn logging: where the context comes from, and token counts."""

from __future__ import annotations

import json
import logging
from dataclasses import replace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.model import Usage
from alfred.history import History
from alfred.tools.base import TurnInput
from tests.agent.turn_helpers import COOKBOOK, MCPARR, agent, owner_turn
from tests.helpers import FakeModel, calls, make_agent, make_config, text
from tests.mcp.servers import COOKBOOK_INSTRUCTIONS, in_process, make_cookbook

# --- context size and token logging ---------------------------------------------


def turn_lines(caplog: pytest.LogCaptureFixture, turn: TurnInput, what: str) -> list[str]:
    prefix = f"turn {turn.queue_item_id} {what}"
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "alfred.agent.turn" and r.getMessage().startswith(prefix)
    ]


def size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False))


async def test_context_size_line(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    history = History(sessions)
    await history.append("diogo", "user", "my private earlier message")
    await history.append("diogo", "assistant", "a private reply")
    model = FakeModel([text("ok")])
    turn = await owner_turn(sessions, "hi")
    with caplog.at_level(logging.INFO):
        await agent(sessions, model).run(turn)

    messages, tools = model.requests[0]

    def group(prefix: str) -> int:
        return sum(size(t) for t in tools if t["function"]["name"].startswith(prefix))

    mcparr, cookbook, local = group("mcparr_"), group("cookbook_"), group("alfred_")
    assert mcparr and cookbook and local
    expected = (
        f"turn {turn.queue_item_id} context: "
        f"system {len(messages[0]['content'])} chars "
        f"(facts off; instructions: cookbook {len(COOKBOOK_INSTRUCTIONS)}); "
        f"tools {len(tools)} defs {mcparr + cookbook + local} chars "
        f"(mcparr {mcparr}, cookbook {cookbook}, alfred {local}); "
        f"history 3 entries {size(messages[1:])} chars; "
        "brain gemma4:26b-a4b-it-qat"
    )
    assert turn_lines(caplog, turn, "context:") == [expected]
    for private in ("my private earlier message", "a private reply", COOKBOOK_INSTRUCTIONS):
        assert private not in expected


async def test_context_size_line_skips_unavailable_server(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    cfg = make_config(mcp_servers=[MCPARR, COOKBOOK])
    connect = in_process({"mcparr": ConnectionError("down"), "cookbook": make_cookbook()})
    turn = await owner_turn(sessions, "hi")
    with caplog.at_level(logging.INFO):
        await make_agent(cfg, FakeModel([text("ok")]), History(sessions), sessions, connect).run(
            turn
        )
    [line] = turn_lines(caplog, turn, "context:")
    assert "cookbook" in line and "mcparr" not in line


async def test_largest_tools_at_debug(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    model = FakeModel([text("ok")])
    turn = await owner_turn(sessions, "hi")
    with caplog.at_level(logging.DEBUG, logger="alfred.agent.turn"):
        await agent(sessions, model).run(turn)
    tools = model.requests[0][1]
    assert len(tools) > 5
    sizes = sorted(((t["function"]["name"], size(t)) for t in tools), key=lambda t: -t[1])
    listed = ", ".join(f"{name} {n}" for name, n in sizes[:5])
    assert turn_lines(caplog, turn, "largest tools:") == [
        f"turn {turn.queue_item_id} largest tools: {listed}"
    ]


async def test_largest_tools_not_at_info(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    turn = await owner_turn(sessions, "hi")
    with caplog.at_level(logging.INFO):
        await agent(sessions, FakeModel([text("ok")])).run(turn)
    assert turn_lines(caplog, turn, "largest tools:") == []


async def test_token_counts_per_request(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    model = FakeModel(
        [
            replace(calls(("mcparr_get_downloads", "{}")), usage=Usage(6812, 38)),
            replace(text("Dune is at 50%."), usage=Usage(7240, 112)),
        ]
    )
    turn = await owner_turn(sessions, "what's downloading?")
    with caplog.at_level(logging.INFO):
        await agent(sessions, model).run(turn)
    t = turn.queue_item_id
    assert turn_lines(caplog, turn, "request") == [
        f"turn {t} request 1: 6812 prompt tokens, 38 completion tokens",
        f"turn {t} request 2: 7240 prompt tokens, 112 completion tokens",
    ]


async def test_token_counts_not_reported(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    turn = await owner_turn(sessions, "hi")
    with caplog.at_level(logging.INFO):
        result = await agent(sessions, FakeModel([text("Hello!")])).run(turn)
    assert result.reply == "Hello!"
    assert turn_lines(caplog, turn, "request") == [
        f"turn {turn.queue_item_id} request 1: token counts not reported"
    ]


# --- images (change add-attachments, design D11) ----------------------------------


def test_images_are_counted_not_measured() -> None:
    from alfred.agent.context import context_sizes

    image = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + "A" * 50_000}}
    text_part = {"type": "text", "text": "what's the total?"}
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": [text_part, image, image]},
        {"role": "assistant", "content": "12.40 EUR."},
    ]
    line = context_sizes(messages, [])
    without = [
        {"role": "user", "content": [text_part]},
        {"role": "assistant", "content": "12.40 EUR."},
    ]
    assert line.endswith(f"history 2 entries {size(without)} chars, 2 images")
    assert size(without) < 1000  # the base64 isn't counted


def test_one_image() -> None:
    from alfred.agent.context import context_sizes

    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": [image]}]
    assert context_sizes(messages, []).endswith(", 1 image")


def test_no_images_no_count() -> None:
    from alfred.agent.context import context_sizes

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    assert context_sizes(messages, []).endswith("chars")


# --- the user's facts (change add-user-facts, design D10) ---------------------------------


def test_facts_in_the_system_part() -> None:
    from alfred.agent.context import context_sizes

    messages = [{"role": "system", "content": "s" * 9120}]
    assert context_sizes(messages, [], (14, 14, 1240)).startswith(
        "system 9120 chars (facts 14, 1240 chars); "
    )
    assert context_sizes(messages, []).startswith("system 9120 chars (facts off); ")
    assert context_sizes(messages, [], (0, 0, 0)).startswith("system 9120 chars (facts none); ")
    assert context_sizes(messages, [], (40, 52, 2990)).startswith(
        "system 9120 chars (facts 40 of 52, 2990 chars, 12 left out); "
    )


async def _facts_turn(
    sessions: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
    count: int,
    budget: int = 3000,
) -> tuple[TurnInput, FakeModel]:
    from alfred.memory.facts import FactStore, FactTools
    from tests.helpers import FakeEmbedder

    cfg = make_config(memory={"facts_in_context": True})
    store = FactStore(cfg, sessions, FakeEmbedder(default=[1.0]))
    for i in range(count):
        await store.add("diogo", f"secret fact {i:02}", "learned")
    model = FakeModel([text("ok")])
    turn = await owner_turn(sessions, "hi")
    memory = {"facts_in_context": True, "facts_context_max_chars": budget}
    with caplog.at_level(logging.INFO):
        await agent(sessions, model, facts=FactTools(store, cfg.memory), memory_config=memory).run(
            turn
        )
    return turn, model


async def test_facts_logged_without_their_text(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    turn, model = await _facts_turn(sessions, caplog, 14)
    prompt = model.requests[0][0][0]["content"]
    block = prompt[prompt.index("What you know about") :]
    [line] = turn_lines(caplog, turn, "context")
    assert f"(facts 14, {len(block)} chars; instructions:" in line
    assert "secret fact" not in caplog.text
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


async def test_facts_over_budget_warns(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    line_size = len("[10] secret fact 09 (learned)") + 1
    turn, _ = await _facts_turn(sessions, caplog, 52, budget=40 * line_size)
    [line] = turn_lines(caplog, turn, "context")
    assert "(facts 40 of 52, " in line and ", 12 left out; " in line
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        f"turn {turn.queue_item_id}: 40 of 52 facts fit [memory] facts_context_max_chars "
        f"{40 * line_size}"
    ]


async def test_no_facts_yet(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    turn, _ = await _facts_turn(sessions, caplog, 0)
    [line] = turn_lines(caplog, turn, "context")
    assert "(facts none; instructions:" in line
