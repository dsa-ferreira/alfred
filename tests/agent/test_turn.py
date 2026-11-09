from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.agent.brains import SET_MODEL
from alfred.agent.conversation import END_CONVERSATION
from alfred.agent.model import ModelFailed, ModelUnreachable
from alfred.agent.prompts import NO_REPLY, brain_line, butler_persona
from alfred.agent.toolset import AttemptContext, LocalToolset
from alfred.agent.turn import Agent, TurnFailed, TurnInput
from alfred.history import History
from alfred.memory.search import RECALL_CONVERSATION, SEARCH_CONVERSATIONS
from alfred.web.tools import WEB_FETCH, WEB_SEARCH
from tests.agent.turn_helpers import (
    BAD_MODEL,
    COOKBOOK,
    COOKBOOK_SECTION,
    GEMMA,
    MCPARR,
    NOW,
    RULES,
    Clock,
    agent,
    memory,
    owner_turn,
    reminder_tools,
    tool_rows,
    two_brains,
    web_tools,
)
from tests.helpers import (
    FakeEmbedder,
    FakeModel,
    add_queue_item,
    calls,
    make_agent,
    make_config,
    text,
)
from tests.mcp.servers import COOKBOOK_INSTRUCTIONS, in_process, make_cookbook

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
    await make_agent(cfg, model, History(sessions), sessions, connect).run(
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
    await make_agent(cfg, model, History(sessions), sessions, connect).run(
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


# --- today's date ------------------------------------------------------------------


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


# --- fallback --------------------------------------------------------------------


FELL_BACK = notices.fell_back("sir", "bonsai", "gemma")


async def test_fallback_before_any_tool_runs_the_turn_again(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    brains = two_brains("bonsai")
    model = FakeModel([BAD_MODEL, text("gemma's answer")])
    with caplog.at_level(logging.WARNING):
        result = await agent(sessions, model, brains=brains).run(await owner_turn(sessions, "hi"))
    assert result.reply == "gemma's answer" and result.fallback_notice == FELL_BACK
    assert model.models == ["bonsai:8b", GEMMA]
    assert brains.current.name == "gemma"
    retry = model.requests[1][0]
    assert brain_line("gemma", GEMMA, ["bonsai"]) in retry[0]["content"]
    assert [m["content"] for m in retry[1:]] == ["hi"]
    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "bonsai" in warning and "gemma" in warning and "does not support tools" in warning


async def test_fallback_after_a_tool_ran(sessions: async_sessionmaker[AsyncSession]) -> None:
    brains = two_brains("bonsai")
    model = FakeModel([calls(("mcparr_get_downloads", "{}")), BAD_MODEL])
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model, brains=brains).run(await owner_turn(sessions, "downloads?"))
    assert exc.value.notice == notices.failed("sir", True)
    assert exc.value.fallback_notice == FELL_BACK
    assert not exc.value.retryable and exc.value.side_effects_possible
    assert model.models == ["bonsai:8b", "bonsai:8b"]
    assert brains.current.name == "gemma"


async def test_startup_brain_fails_after_a_fallback(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([BAD_MODEL, BAD_MODEL])
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model, brains=two_brains("bonsai")).run(
            await owner_turn(sessions, "hi")
        )
    assert exc.value.notice == notices.failed("sir")
    assert exc.value.fallback_notice == FELL_BACK
    assert model.models == ["bonsai:8b", GEMMA]


async def test_startup_brain_failure_has_no_fallback(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([BAD_MODEL])
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model, brains=two_brains()).run(await owner_turn(sessions, "hi"))
    assert exc.value.fallback_notice is None and model.models == [GEMMA]


async def test_unreachable_server_does_not_fall_back(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    brains = two_brains("bonsai")
    model = FakeModel([ModelUnreachable("can't reach the model server (ConnectError)")])
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model, brains=brains).run(await owner_turn(sessions, "hi"))
    assert exc.value.retryable and exc.value.fallback_notice is None
    assert brains.current.name == "bonsai"


async def test_ask_turn_falls_back(sessions: async_sessionmaker[AsyncSession]) -> None:
    brains = two_brains("bonsai")
    item = await add_queue_item(sessions, "ask", "anything?", "disk-check")
    model = FakeModel([BAD_MODEL, text(NO_REPLY)])
    result = await agent(sessions, model, brains=brains).run(
        TurnInput("ask", "anything?", item, "disk-check")
    )
    assert result.reply == NO_REPLY and result.fallback_notice == FELL_BACK
    assert brains.current.name == "gemma"


# --- the full tool list (pinned before change refactor-agent-toolsets) ----------


MCP_TOOLS = [
    "mcparr_get_downloads",
    "mcparr_check_health",
    "mcparr_add_media",
    "cookbook_get_recipe",
    "cookbook_delete_recipe",
    "cookbook_check_health",
    "cookbook_big",
]


def all_toolsets_agent(
    sessions: async_sessionmaker[AsyncSession], model: FakeModel, http: httpx.AsyncClient
) -> Agent:
    return agent(
        sessions,
        model,
        memory=memory(sessions, FakeEmbedder()),
        web=web_tools(http),
        reminders=reminder_tools(sessions, Clock(NOW)),
        brains=two_brains(),
    )


async def test_tool_order_on_owner_turns(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    model = FakeModel([text("ok")])
    await all_toolsets_agent(sessions, model, web_http).run(await owner_turn(sessions, "hi"))
    assert [t["function"]["name"] for t in model.requests[0][1]] == [
        WEB_SEARCH,
        WEB_FETCH,
        END_CONVERSATION,
        SEARCH_CONVERSATIONS,
        RECALL_CONVERSATION,
        "alfred_remind_set",
        "alfred_reminders_list",
        "alfred_reminder_cancel",
        SET_MODEL,
        *MCP_TOOLS,
    ]


async def test_tool_order_on_ask_turns(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    model = FakeModel([text(NO_REPLY)])
    item = await add_queue_item(sessions, "ask", "93%", "disk-check")
    await all_toolsets_agent(sessions, model, web_http).run(
        TurnInput("ask", "93%", item, "disk-check")
    )
    assert [t["function"]["name"] for t in model.requests[0][1]] == [
        WEB_SEARCH,
        WEB_FETCH,
        *MCP_TOOLS,
    ]


async def test_toolsets_opened_once_and_told_of_every_attempt(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    class Recorder(LocalToolset):
        def __init__(self) -> None:
            self.opened = 0
            self.attempts: list[AttemptContext] = []

        async def open(self, turn: TurnInput) -> LocalToolset:
            self.opened += 1
            return self

        def new_attempt(self, attempt: AttemptContext) -> None:
            self.attempts.append(attempt)

    recorder = Recorder()
    brains = two_brains("bonsai")
    model = FakeModel([BAD_MODEL, text("gemma's answer")])
    a = Agent(make_config(), model, History(sessions), sessions, toolsets=[recorder], brains=brains)
    await a.run(await owner_turn(sessions, "hi"))
    assert recorder.opened == 1  # the MCP servers are listed once per turn
    assert len(recorder.attempts) == 2
    assert recorder.attempts[0] is not recorder.attempts[1]
    assert [ctx.owner_texts for ctx in recorder.attempts] == [["hi"], ["hi"]]
