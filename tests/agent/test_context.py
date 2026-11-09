"""Turn logging: where the context comes from, and token counts."""

from __future__ import annotations

import json
import logging
from dataclasses import replace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.model import Usage
from alfred.agent.turn import TurnInput
from alfred.history import History
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
    await history.append("owner", "my private earlier message")
    await history.append("assistant", "a private reply")
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
        f"(instructions: cookbook {len(COOKBOOK_INSTRUCTIONS)}); "
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
