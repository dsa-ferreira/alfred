from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred import notices
from alfred.agent.brains import Brains
from alfred.agent.model import ModelFailed, ModelReply, ModelUnreachable, Usage
from alfred.agent.prompts import (
    NO_REPLY,
    OPERATING_RULES,
    brain_line,
    butler_persona,
    server_instructions,
)
from alfred.agent.turn import (
    END_CONVERSATION,
    RECALL_CONVERSATION,
    SEARCH_CONVERSATIONS,
    SET_MODEL,
    Agent,
    TurnFailed,
    TurnInput,
)
from alfred.db.models import Conversation, Entry, TurnToolCall
from alfred.history import History
from alfred.mcp.registry import ServerInstructions
from alfred.memory.service import Memory
from alfred.web.fetch import Fetcher, make_http_client
from alfred.web.search import SearxngClient
from alfred.web.tools import WEB_FETCH, WEB_SEARCH, WebTools
from tests.helpers import FakeEmbedder, FakeModel, add_queue_item, calls, make_config, text
from tests.mcp.servers import COOKBOOK_INSTRUCTIONS, in_process, make_cookbook, make_mcparr
from tests.mcp.servers import config as server_config
from tests.memory.test_search import CHICKEN, at, seed
from tests.web.test_fetch import Resolver
from tests.web.test_search import SEARXNG
from tests.web.test_search import reply as search_reply

MCPARR = server_config("mcparr")
COOKBOOK = server_config("cookbook")
# mcparr sends no instructions, so cookbook's is the only server section.
COOKBOOK_SECTION = server_instructions(ServerInstructions("cookbook", COOKBOOK_INSTRUCTIONS))
# Monday 2 November 2026, 14:05 in Lisbon (WET, UTC+0).
NOW = datetime(2026, 11, 2, 14, 5, tzinfo=UTC)
RULES = (
    OPERATING_RULES
    + "\n\nToday is Monday, 2 November 2026."
    + "\n\nYou are running on the model gemma4:26b-a4b-it-qat."
)


def agent(
    sessions: async_sessionmaker[AsyncSession],
    model: FakeModel,
    *,
    memory: Memory | None = None,
    web: WebTools | None = None,
    brains: Brains | None = None,
    now: datetime = NOW,
    **config: object,
) -> Agent:
    cfg = make_config(mcp_servers=[MCPARR, COOKBOOK], **config)  # type: ignore[arg-type]
    connect = in_process({"mcparr": make_mcparr(), "cookbook": make_cookbook()})
    return Agent(
        cfg,
        model,
        History(sessions),
        sessions,
        connect,
        memory=memory,
        web=web,
        brains=brains,
        now=lambda: now,
    )


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
        await Agent(cfg, FakeModel([text("ok")]), History(sessions), sessions, connect).run(turn)
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


# --- brains ----------------------------------------------------------------------

GEMMA = "gemma4:26b-a4b-it-qat"
BRAINS = {"gemma": GEMMA, "bonsai": "bonsai:8b"}
BAD_MODEL = ModelFailed("model server answered HTTP 400: model does not support tools")


def two_brains(current: str = "gemma") -> Brains:
    brains = Brains(GEMMA, BRAINS)
    brains.set(current)
    return brains


def set_model(name: str) -> object:
    return calls((SET_MODEL, json.dumps({"model": name})))


def tool_result(model: FakeModel, request: int) -> str:
    return model.requests[request][0][-1]["content"]


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


# --- web tools (change add-web-search) ----------------------------------------------


@pytest.fixture
async def web_http() -> AsyncIterator[httpx.AsyncClient]:
    async with make_http_client() as client:
        yield client


def web_tools(http: httpx.AsyncClient, *, page_chars: int = 8000) -> WebTools:
    search = SearxngClient(http, SEARXNG, results=5, timeout_s=5, max_chars=4000)
    fetcher = Fetcher(http, timeout_s=5, max_bytes=5_000_000, resolver=Resolver())
    return WebTools(search, fetcher, page_chars=page_chars)


def fetch_call(url: str, **extra: object) -> ModelReply:
    return calls((WEB_FETCH, json.dumps({"url": url, **extra})))


def search_call(query: str) -> ModelReply:
    return calls((WEB_SEARCH, json.dumps({"query": query})))


def last_tool_result(model: FakeModel) -> str:
    return model.requests[-1][0][-1]["content"]


async def test_web_tools_offered_in_owner_turns(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, web=web_tools(web_http)).run(await owner_turn(sessions, "hi"))
    names = [t["function"]["name"] for t in model.requests[0][1]]
    assert {WEB_SEARCH, WEB_FETCH} <= set(names)
    # Ahead of the owner-only local tools, which come before the MCP tools.
    assert names[:3] == [WEB_SEARCH, WEB_FETCH, END_CONVERSATION]


async def test_web_tools_offered_in_ask_turns(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    item = await add_queue_item(sessions, "ask", "anything new?", "backup")
    model = FakeModel([text(NO_REPLY)])
    await agent(sessions, model, web=web_tools(web_http)).run(
        TurnInput("ask", "anything new?", item, "backup")
    )
    assert {WEB_SEARCH, WEB_FETCH} <= model.tool_names()
    assert END_CONVERSATION not in model.tool_names()


async def test_no_web_tools_without_web(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model).run(await owner_turn(sessions, "hi"))
    assert not {WEB_SEARCH, WEB_FETCH} & model.tool_names()


@respx.mock
async def test_url_the_owner_pasted(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    route = respx.get("https://example.com/article").respond(
        200, headers={"content-type": "text/plain"}, text="The article text."
    )
    model = FakeModel([fetch_call("https://example.com/article"), text("It says hi.")])
    turn = await owner_turn(sessions, "summarise https://example.com/article")
    result = await agent(sessions, model, web=web_tools(web_http)).run(turn)
    assert result.reply == "It says hi."
    assert route.called
    assert "The article text." in last_tool_result(model)
    rows = await tool_rows(sessions)
    assert [(r.tool, r.ok, r.arguments) for r in rows] == [
        (WEB_FETCH, True, {"url": "https://example.com/article"})
    ]


@respx.mock
async def test_url_the_owner_pasted_in_an_earlier_message(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    history = History(sessions)
    await history.append("owner", "have a look at https://example.com/pricing-page.")
    await history.append("assistant", "It is a product page.")
    respx.get("https://example.com/pricing-page").respond(
        200, headers={"content-type": "text/plain"}, text="Pro plan: 10 euros."
    )
    model = FakeModel([fetch_call("https://example.com/pricing-page"), text("10 euros.")])
    turn = await owner_turn(sessions, "and what does it say about pricing?")
    await agent(sessions, model, web=web_tools(web_http)).run(turn)
    assert "Pro plan: 10 euros." in last_tool_result(model)


@respx.mock
async def test_owner_url_outside_the_window_refused(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    history = History(sessions)
    await history.append("owner", "see https://example.com/old")
    await history.append("assistant", "Seen.")
    route = respx.get("https://example.com/old").respond(200, text="x")
    model = FakeModel([fetch_call("https://example.com/old"), text("Can't.")])
    turn = await owner_turn(sessions, "open it again")
    await agent(sessions, model, web=web_tools(web_http), history={"max_entries": 2}).run(turn)
    assert "Search for it first" in last_tool_result(model)
    assert not route.called


@respx.mock
async def test_url_in_the_ask_prompt(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    respx.get("https://example.com/release").respond(
        200, headers={"content-type": "text/plain"}, text="Version 2 is out."
    )
    prompt = "New release notes at https://example.com/release. Worth telling?"
    item = await add_queue_item(sessions, "ask", prompt, "updates")
    model = FakeModel([fetch_call("https://example.com/release"), text(NO_REPLY)])
    await agent(sessions, model, web=web_tools(web_http)).run(
        TurnInput("ask", prompt, item, "updates")
    )
    assert "Version 2 is out." in last_tool_result(model)


@respx.mock
async def test_search_result_from_an_earlier_turn(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    data = search_reply()
    listed = data["results"][1]["url"]
    respx.get(f"{SEARXNG}/search").respond(json=data)
    page = respx.get(listed).respond(200, headers={"content-type": "text/plain"}, text="x")
    web = web_tools(web_http)

    first = FakeModel([search_call("lisbon weather"), text("Sunny, per BBC.")])
    await agent(sessions, first, web=web).run(await owner_turn(sessions, "lisbon weather?"))
    assert listed in last_tool_result(first)
    await History(sessions).append("assistant", "Sunny, per BBC.")

    second = FakeModel([fetch_call(listed), text("Can't open it.")])
    await agent(sessions, second, web=web).run(await owner_turn(sessions, "open the BBC page"))
    assert "Search for it first" in last_tool_result(second)
    assert not page.called


@respx.mock
async def test_url_from_alfreds_own_reply(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    history = History(sessions)
    await history.append("owner", "where are the docs?")
    await history.append("assistant", "At https://example.com/docs, I believe.")
    route = respx.get("https://example.com/docs").respond(200, text="x")
    model = FakeModel([fetch_call("https://example.com/docs"), text("Searching instead.")])
    await agent(sessions, model, web=web_tools(web_http)).run(
        await owner_turn(sessions, "open that")
    )
    assert "Search for it first" in last_tool_result(model)
    assert not route.called


@respx.mock
async def test_url_in_a_notice_or_recall(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    history = History(sessions)
    await history.append("notice", "Report at https://example.com/notice-report", source="backup")
    current = await history.current_conversation_id()
    async with sessions.begin() as session:
        old = Conversation(ended_at=NOW, title="Old chat", summary="Links.")
        session.add(old)
        await session.flush()
        session.add(
            Entry(
                conversation_id=current,
                kind="recall",
                text="owner: see https://example.com/recalled",
                recalled_conversation_id=old.id,
            )
        )
    routes = [
        respx.get("https://example.com/notice-report").respond(200, text="x"),
        respx.get("https://example.com/recalled").respond(200, text="x"),
    ]
    model = FakeModel(
        [
            fetch_call("https://example.com/notice-report"),
            fetch_call("https://example.com/recalled"),
            text("Neither opens."),
        ]
    )
    await agent(sessions, model, web=web_tools(web_http)).run(
        await owner_turn(sessions, "open those links")
    )
    results = [m["content"] for m in model.requests[-1][0] if m["role"] == "tool"]
    assert len(results) == 2 and all("Search for it first" in r for r in results)
    assert not any(r.called for r in routes)


@respx.mock
async def test_fetch_result_not_cut_by_the_global_limit(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    respx.get("https://example.com/long").respond(
        200, headers={"content-type": "text/plain"}, text="w" * 20000
    )
    model = FakeModel([fetch_call("https://example.com/long"), text("Long.")])
    turn = await owner_turn(sessions, "read https://example.com/long")
    await agent(sessions, model, web=web_tools(web_http), ai={"tool_result_max_chars": 1000}).run(
        turn
    )
    result = last_tool_result(model)
    assert result.count("w") >= 8000
    assert "[truncated" not in result


@respx.mock
async def test_web_failure_goes_to_the_model(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    respx.get(f"{SEARXNG}/search").mock(side_effect=httpx.ConnectError("down"))
    model = FakeModel([search_call("news"), text("Search is down, I'm afraid.")])
    result = await agent(sessions, model, web=web_tools(web_http)).run(
        await owner_turn(sessions, "any news?")
    )
    assert result.reply == "Search is down, I'm afraid."
    assert last_tool_result(model).startswith("Error: web search is unavailable")


@respx.mock
async def test_room_for_web_research(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    first = search_reply()
    urls = [f"https://example.com/result-{n}" for n in range(3)]
    for result, url in zip(first["results"], urls, strict=False):
        result["url"] = url
    second = search_reply()
    second["results"][0]["url"] = "https://example.com/second-search"
    respx.get(f"{SEARXNG}/search").mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    plain = {"content-type": "text/plain"}
    respx.get(urls[0]).respond(200, headers=plain, text="a" * 20000)
    respx.get(urls[1]).respond(200, headers=plain, text="b" * 3000)
    respx.get("https://example.com/second-search").respond(200, headers=plain, text="c" * 3000)
    model = FakeModel(
        [
            search_call("lisbon weather"),
            fetch_call(urls[0]),
            fetch_call(urls[1]),
            fetch_call(urls[0], start=8000),
            search_call("lisbon weather tomorrow"),
            fetch_call("https://example.com/second-search"),
            text("Sunny today and tomorrow."),
        ]
    )
    result = await agent(sessions, model, web=web_tools(web_http)).run(
        await owner_turn(sessions, "weather in lisbon this week?")
    )
    assert make_config().ai.max_requests_per_turn == 12
    assert result.reply == "Sunny today and tomorrow."
    assert len(model.requests) == 7
    assert all(r.ok for r in await tool_rows(sessions))
