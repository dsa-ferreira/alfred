from __future__ import annotations

import logging

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.model import ModelFailed, ModelUnreachable
from alfred.agent.prompts import DEFAULT_SYSTEM_PROMPT, NO_REPLY
from alfred.agent.turn import END_CONVERSATION, Agent, TurnFailed, TurnInput
from alfred.db.models import TurnToolCall
from alfred.history import History
from tests.helpers import FakeModel, add_queue_item, calls, make_config, text
from tests.mcp.servers import config as server_config
from tests.mcp.servers import in_process, make_cookbook, make_mcparr

MCPARR = server_config("mcparr")
COOKBOOK = server_config("cookbook")


def agent(sessions: async_sessionmaker[AsyncSession], model: FakeModel, **config: object) -> Agent:
    cfg = make_config(mcp_servers=[MCPARR, COOKBOOK], **config)  # type: ignore[arg-type]
    connect = in_process({"mcparr": make_mcparr(), "cookbook": make_cookbook()})
    return Agent(cfg, model, History(sessions), sessions, connect)


async def owner_turn(sessions: async_sessionmaker[AsyncSession], message: str) -> TurnInput:
    item = await add_queue_item(sessions, "owner", message)
    await History(sessions).append("owner", message, queue_item_id=item)
    return TurnInput("owner", message, item)


async def tool_rows(sessions: async_sessionmaker[AsyncSession]) -> list[TurnToolCall]:
    async with sessions() as session:
        return list(await session.scalars(select(TurnToolCall).order_by(TurnToolCall.id)))


# --- prompt and context --------------------------------------------------------


async def test_default_prompt_and_context(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    await history.append("owner", "earlier")
    await history.append("assistant", "earlier reply")
    model = FakeModel([text("Hello!")])
    result = await agent(sessions, model).run(await owner_turn(sessions, "hi"))
    assert result.reply == "Hello!" and not result.reset_requested
    messages = model.requests[0][0]
    assert messages[0] == {"role": "system", "content": DEFAULT_SYSTEM_PROMPT}
    assert [m["content"] for m in messages[1:]] == ["earlier", "earlier reply", "hi"]


async def test_custom_prompt(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, ai={"system_prompt": "Be terse."}).run(
        await owner_turn(sessions, "hi")
    )
    assert model.requests[0][0][0]["content"] == "Be terse."


async def test_context_window_limit(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    for i in range(10):
        await history.append("owner", f"m{i}")
    model = FakeModel([text("ok")])
    await agent(sessions, model, history={"max_entries": 3}).run(await owner_turn(sessions, "last"))
    assert [m["content"] for m in model.requests[0][0][1:]] == ["m8", "m9", "last"]


async def test_ask_turn_context(sessions: async_sessionmaker[AsyncSession]) -> None:
    await History(sessions).append("owner", "earlier")
    item = await add_queue_item(sessions, "ask", "is this worth telling?", "disk-check")
    model = FakeModel([text(NO_REPLY)])
    result = await agent(sessions, model).run(
        TurnInput("ask", "is this worth telling?", item, "disk-check")
    )
    assert result.reply == NO_REPLY
    messages = model.requests[0][0]
    assert NO_REPLY in messages[0]["content"]
    assert messages[1]["content"] == "earlier"
    assert messages[-1] == {"role": "user", "content": "[ask:disk-check] is this worth telling?"}


async def test_unavailable_servers_told(sessions: async_sessionmaker[AsyncSession]) -> None:
    cfg = make_config(mcp_servers=[MCPARR, COOKBOOK])
    connect = in_process({"mcparr": ConnectionError("down"), "cookbook": make_cookbook()})
    model = FakeModel([text("ok")])
    await Agent(cfg, model, History(sessions), sessions, connect).run(
        await owner_turn(sessions, "hi")
    )
    assert "unavailable" in model.requests[0][0][0]["content"]
    assert "mcparr" in model.requests[0][0][0]["content"]
    assert not any(n.startswith("mcparr_") for n in model.tool_names())


# --- tool-call loop ------------------------------------------------------------


async def test_one_tool_call(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    model = FakeModel([calls(("mcparr_get_downloads", "{}")), text("Dune is at 50%.")])
    turn = await owner_turn(sessions, "what's downloading?")
    with caplog.at_level(logging.INFO):
        result = await agent(sessions, model).run(turn)
    assert result.reply == "Dune is at 50%."
    assert result.side_effects_possible
    second = model.requests[1][0]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "mcparr_get_downloads"
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "c0"
    assert "Dune" in second[-1]["content"]

    rows = await tool_rows(sessions)
    assert [(r.queue_item_id, r.seq, r.tool, r.ok) for r in rows] == [
        (turn.queue_item_id, 1, "mcparr_get_downloads", True)
    ]
    assert "mcparr_get_downloads" in caplog.text


async def test_sequential_calls_in_order(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel(
        [
            calls(("mcparr_add_media", '{"title": "Dune"}'), ("mcparr_check_health", "{}")),
            text("done"),
        ]
    )
    await agent(sessions, model).run(await owner_turn(sessions, "add Dune"))
    rows = await tool_rows(sessions)
    assert [(r.seq, r.tool, r.arguments) for r in rows] == [
        (1, "mcparr_add_media", {"title": "Dune"}),
        (2, "mcparr_check_health", {}),
    ]
    results = [m for m in model.requests[1][0] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in results] == ["c0", "c1"]


async def test_tool_error_goes_to_model(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([calls(("cookbook_get_recipe", '{"recipe_id": 9}')), text("No such recipe.")])
    result = await agent(sessions, model).run(await owner_turn(sessions, "recipe 9?"))
    assert result.reply == "No such recipe."
    tool_message = model.requests[1][0][-1]
    assert "recipe 9 does not exist" in tool_message["content"]
    assert (await tool_rows(sessions))[0].ok is False


async def test_unknown_tool(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([calls(("made_up_tool", "{}")), text("Sorry.")])
    await agent(sessions, model).run(await owner_turn(sessions, "hi"))
    assert "unknown tool" in model.requests[1][0][-1]["content"]


async def test_invalid_arguments_stored_raw(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([calls(("mcparr_add_media", "{oops")), text("Sorry.")])
    await agent(sessions, model).run(await owner_turn(sessions, "hi"))
    assert (await tool_rows(sessions))[0].arguments == {"_raw": "{oops"}


# --- limits and failures -------------------------------------------------------


async def test_runaway_tool_use(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([calls(("mcparr_check_health", "{}"))] * 3)
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model, ai={"max_requests_per_turn": 3}).run(
            await owner_turn(sessions, "loop")
        )
    assert len(model.requests) == 3
    assert "too many steps" in exc.value.notice
    assert not exc.value.retryable


async def test_unreachable_before_tools_is_retryable(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([ModelUnreachable("refused")])
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model).run(await owner_turn(sessions, "hi"))
    assert exc.value.retryable and not exc.value.side_effects_possible


async def test_unreachable_after_tool_is_not_retried(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([calls(("mcparr_add_media", '{"title": "Dune"}')), ModelUnreachable("x")])
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model).run(await owner_turn(sessions, "add Dune"))
    assert not exc.value.retryable and exc.value.side_effects_possible
    assert "partly completed" in exc.value.notice


async def test_model_failure(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([ModelFailed("HTTP 400")])
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model).run(await owner_turn(sessions, "hi"))
    assert not exc.value.retryable and "HTTP 400" in exc.value.reason
    assert "400" not in exc.value.notice


async def test_empty_reply_fails(sessions: async_sessionmaker[AsyncSession]) -> None:
    with pytest.raises(TurnFailed):
        await agent(sessions, FakeModel([text("  ")])).run(await owner_turn(sessions, "hi"))


# --- alfred_end_conversation ---------------------------------------------------


async def test_end_conversation_offered_on_owner_turns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model).run(await owner_turn(sessions, "hi"))
    assert END_CONVERSATION in model.tool_names()
    assert "mcparr_get_downloads" in model.tool_names()
    description = next(
        t["function"]["description"]
        for t in model.requests[0][1]
        if t["function"]["name"] == END_CONVERSATION
    )
    assert "ONLY" in description


async def test_end_conversation_not_offered_on_ask_turns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    item = await add_queue_item(sessions, "ask", "x", "disk-check")
    model = FakeModel([calls((END_CONVERSATION, "{}")), text(NO_REPLY)])
    result = await agent(sessions, model).run(TurnInput("ask", "x", item, "disk-check"))
    assert END_CONVERSATION not in model.tool_names()
    assert not result.reset_requested
    assert "unknown tool" in model.requests[1][0][-1]["content"]


async def test_end_conversation_marks_reset(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([calls((END_CONVERSATION, "{}")), text("Done 👍")])
    result = await agent(sessions, model).run(await owner_turn(sessions, "we're done here"))
    assert result.reply == "Done 👍"
    assert result.reset_requested
    # The reset itself happens after delivery, not during the turn.
    assert [e.text for e in await History(sessions).recent(30)] == ["we're done here"]
