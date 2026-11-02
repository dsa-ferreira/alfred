from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.agent.model import ModelFailed, ModelUnreachable
from alfred.agent.prompts import NO_REPLY, OPERATING_RULES, butler_persona, server_instructions
from alfred.agent.turn import (
    END_CONVERSATION,
    RECALL_CONVERSATION,
    SEARCH_CONVERSATIONS,
    Agent,
    TurnFailed,
    TurnInput,
)
from alfred.db.models import Entry, TurnToolCall
from alfred.history import History
from alfred.mcp.registry import ServerInstructions
from alfred.memory.service import Memory
from tests.helpers import FakeEmbedder, FakeModel, add_queue_item, calls, make_config, text
from tests.mcp.servers import COOKBOOK_INSTRUCTIONS, in_process, make_cookbook, make_mcparr
from tests.mcp.servers import config as server_config
from tests.memory.test_search import CHICKEN, at, seed

MCPARR = server_config("mcparr")
COOKBOOK = server_config("cookbook")
# mcparr sends no instructions, so cookbook's is the only server section.
COOKBOOK_SECTION = server_instructions(ServerInstructions("cookbook", COOKBOOK_INSTRUCTIONS))
# Monday 2 November 2026, 14:05 in Lisbon (WET, UTC+0).
NOW = datetime(2026, 11, 2, 14, 5, tzinfo=UTC)
RULES = OPERATING_RULES + "\n\nToday is Monday, 2 November 2026."


def agent(
    sessions: async_sessionmaker[AsyncSession],
    model: FakeModel,
    *,
    memory: Memory | None = None,
    now: datetime = NOW,
    **config: object,
) -> Agent:
    cfg = make_config(mcp_servers=[MCPARR, COOKBOOK], **config)  # type: ignore[arg-type]
    connect = in_process({"mcparr": make_mcparr(), "cookbook": make_cookbook()})
    return Agent(cfg, model, History(sessions), sessions, connect, memory=memory, now=lambda: now)


def memory(sessions: async_sessionmaker[AsyncSession], embedder: FakeEmbedder) -> Memory:
    return Memory(make_config(), sessions, FakeModel(), embedder)


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
    assert messages[0] == {
        "role": "system",
        "content": butler_persona("sir") + "\n\n" + RULES + "\n\n" + COOKBOOK_SECTION,
    }
    assert [m["content"] for m in messages[1:]] == ["earlier", "earlier reply", "hi"]


async def test_custom_persona(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, ai={"persona": "Be terse."}).run(await owner_turn(sessions, "hi"))
    assert model.requests[0][0][0]["content"] == (
        "Be terse.\n\n" + RULES + "\n\n" + COOKBOOK_SECTION
    )


async def test_custom_address(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, ai={"owner_address": "Mr. Ferreira"}).run(
        await owner_turn(sessions, "hi")
    )
    assert model.requests[0][0][0]["content"].startswith(butler_persona("Mr. Ferreira"))


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


async def test_server_instructions_reach_the_model(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model).run(await owner_turn(sessions, "add a recipe"))
    system = model.requests[0][0][0]["content"]
    assert COOKBOOK_INSTRUCTIONS in system
    assert "offered to you as cookbook_<tool>" in system
    assert 'tool server "mcparr"' not in system


async def test_instructions_cap_from_config(sessions: async_sessionmaker[AsyncSession]) -> None:
    cfg = make_config(mcp_servers=[COOKBOOK])
    cfg = replace(cfg, mcp=cfg.mcp.model_copy(update={"instructions_max_chars": 10}))
    model = FakeModel([text("ok")])
    connect = in_process({"cookbook": make_cookbook()})
    await Agent(cfg, model, History(sessions), sessions, connect).run(
        await owner_turn(sessions, "hi")
    )
    system = model.requests[0][0][0]["content"]
    assert COOKBOOK_INSTRUCTIONS[:10] + "\n[truncated:" in system


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
    assert exc.value.notice == notices.too_many_steps("sir", partial=True)
    assert not exc.value.retryable


async def test_unreachable_before_tools_is_retryable(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([ModelUnreachable("refused")])
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model).run(await owner_turn(sessions, "hi"))
    assert exc.value.retryable and not exc.value.side_effects_possible
    assert exc.value.notice == notices.unreachable("sir")


async def test_unreachable_after_tool_is_not_retried(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([calls(("mcparr_add_media", '{"title": "Dune"}')), ModelUnreachable("x")])
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model).run(await owner_turn(sessions, "add Dune"))
    assert not exc.value.retryable and exc.value.side_effects_possible
    assert exc.value.notice == notices.unreachable_mid("sir")


async def test_model_failure(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([ModelFailed("HTTP 400")])
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model).run(await owner_turn(sessions, "hi"))
    assert not exc.value.retryable and "HTTP 400" in exc.value.reason
    assert exc.value.notice == notices.failed("sir")


async def test_failure_notice_uses_configured_address(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([ModelFailed("HTTP 400")])
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model, ai={"owner_address": "Mr. Ferreira"}).run(
            await owner_turn(sessions, "hi")
        )
    assert exc.value.notice.endswith(", Mr. Ferreira.")


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


# --- memory tools ------------------------------------------------------------------


async def test_memory_tools_offered_on_owner_turns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, memory=memory(sessions, FakeEmbedder())).run(
        await owner_turn(sessions, "hi")
    )
    assert {SEARCH_CONVERSATIONS, RECALL_CONVERSATION} <= model.tool_names()
    search = next(
        t["function"] for t in model.requests[0][1] if t["function"]["name"] == SEARCH_CONVERSATIONS
    )
    assert set(search["parameters"]["properties"]) == {"query", "after", "before"}
    assert "required" not in search["parameters"]
    assert "ask the owner which one they meant" in search["description"]
    assert "recall it and tell the owner which conversation you picked" in search["description"]


async def test_memory_tools_not_offered_on_ask_turns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(sessions, 41, "Chicken thighs recipe", "Lemon.", started=at(1))
    item = await add_queue_item(sessions, "ask", "x", "disk-check")
    model = FakeModel([calls((RECALL_CONVERSATION, '{"id": 41}')), text(NO_REPLY)])
    await agent(sessions, model, memory=memory(sessions, FakeEmbedder())).run(
        TurnInput("ask", "x", item, "disk-check")
    )
    assert not {SEARCH_CONVERSATIONS, RECALL_CONVERSATION} & model.tool_names()
    assert "unknown tool" in model.requests[1][0][-1]["content"]
    async with sessions() as session:
        assert list(await session.scalars(select(Entry).where(Entry.kind == "recall"))) == []


async def test_search_then_recall_then_follow_up(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(
        sessions,
        41,
        "Chicken thighs recipe",
        "Lemon-garlic thighs, 40 minutes at 200°C.",
        started=at(1),
        embedding=CHICKEN,
        entries=("chicken thighs tonight?", "40 minutes at 200°C"),
    )
    mem = memory(sessions, FakeEmbedder([CHICKEN]))
    model = FakeModel(
        [
            calls((SEARCH_CONVERSATIONS, '{"query": "chicken recipe"}')),
            calls((RECALL_CONVERSATION, '{"id": 41}')),
            text("That was #41, sir: lemon and garlic."),
            text("40 minutes, sir."),
        ]
    )
    turn = await owner_turn(sessions, "remember the chicken recipe?")
    result = await agent(sessions, model, memory=mem).run(turn)
    assert result.reply == "That was #41, sir: lemon and garlic."

    search_result = model.requests[1][0][-1]
    assert (
        search_result["role"] == "tool"
        and '#41 "Chicken thighs recipe"' in search_result["content"]
    )
    recall_result = model.requests[2][0][-1]["content"]
    assert recall_result.startswith('#41 "Chicken thighs recipe", Sun 1 Nov 2026')
    rows = await tool_rows(sessions)
    assert [(r.tool, r.arguments, r.ok) for r in rows] == [
        (SEARCH_CONVERSATIONS, {"query": "chicken recipe"}, True),
        (RECALL_CONVERSATION, {"id": 41}, True),
    ]

    # The recall stays in the conversation: the next turn still sees it.
    await History(sessions).append("assistant", result.reply, queue_item_id=turn.queue_item_id)
    await agent(sessions, model, memory=mem).run(
        await owner_turn(sessions, "and how long in the oven?")
    )
    contents = [m["content"] for m in model.requests[3][0][1:]]
    assert contents[0] == "remember the chicken recipe?"
    assert contents[1].startswith('[recall:#41 "Chicken thighs recipe"] #41 ')
    assert "Owner: 40 minutes at 200°C" in contents[1]
    assert contents[-1] == "and how long in the oven?"


async def test_memory_tool_errors_reach_the_model(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel(
        [calls((SEARCH_CONVERSATIONS, "{}"), (RECALL_CONVERSATION, '{"id": 9999}')), text("?")]
    )
    await agent(sessions, model, memory=memory(sessions, FakeEmbedder())).run(
        await owner_turn(sessions, "remember?")
    )
    results = [m["content"] for m in model.requests[1][0] if m["role"] == "tool"]
    assert results[0].startswith("Error: give a query")
    assert results[1] == "Error: there is no conversation #9999."
    assert [r.ok for r in await tool_rows(sessions)] == [False, False]


async def test_memory_tool_with_invalid_json(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([calls((RECALL_CONVERSATION, "{oops")), text("?")])
    await agent(sessions, model, memory=memory(sessions, FakeEmbedder())).run(
        await owner_turn(sessions, "remember 41")
    )
    assert "not valid JSON" in model.requests[1][0][-1]["content"]


# --- today's date ------------------------------------------------------------------

# 00:30 in Lisbon on Tuesday 3 November 2026; still 2 November west of it.
JUST_AFTER_MIDNIGHT = datetime(2026, 11, 3, 0, 30, tzinfo=UTC)


async def test_date_in_owner_turn(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, now=JUST_AFTER_MIDNIGHT).run(await owner_turn(sessions, "hi"))
    system = model.requests[0][0][0]["content"]
    assert "Today is Tuesday, 3 November 2026." in system
    assert "00:30" not in system


async def test_date_in_ask_turn(sessions: async_sessionmaker[AsyncSession]) -> None:
    item = await add_queue_item(sessions, "ask", "x", "disk-check")
    model = FakeModel([text(NO_REPLY)])
    await agent(sessions, model, now=JUST_AFTER_MIDNIGHT).run(
        TurnInput("ask", "x", item, "disk-check")
    )
    assert "Today is Tuesday, 3 November 2026." in model.requests[0][0][0]["content"]


async def test_date_follows_the_configured_timezone(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, now=JUST_AFTER_MIDNIGHT, ai={"timezone": "America/New_York"}).run(
        await owner_turn(sessions, "hi")
    )
    assert "Today is Monday, 2 November 2026." in model.requests[0][0][0]["content"]
