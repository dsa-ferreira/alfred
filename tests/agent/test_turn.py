from __future__ import annotations

import base64
import dataclasses
import logging
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent import notices
from alfred.agent.messages import _STAMP
from alfred.agent.prompts import _NO_REPLY, _butler_persona, brain_line, is_no_reply
from alfred.agent.turn import Agent, Fallback, TurnFailed, TurnResult
from alfred.archive.tools import _RECALL_CONVERSATION, _SEARCH_CONVERSATIONS
from alfred.brains.registry import Brains
from alfred.brains.tools import _SET_MODEL
from alfred.conversations.store import History
from alfred.conversations.tools import _END_CONVERSATION
from alfred.kernel.access import _DECLINED, AllowList
from alfred.kernel.content import ModelImage
from alfred.kernel.toolset import AttemptContext, LocalToolset, ToolOutcome
from alfred.kernel.turn import AskTurn, TurnInput
from alfred.llm.chat import ModelFailed, ModelUnreachable
from alfred.reminders.store import Reminders
from alfred.reminders.tools import _REMIND_SET
from alfred.toolbox.calculator import _CALCULATE
from alfred.toolbox.dates import _DATE
from alfred.web.tools import _WEB_FETCH, _WEB_SEARCH
from tests.agent.turn_helpers import (
    BAD_MODEL,
    COOKBOOK,
    COOKBOOK_SECTION,
    DUE,
    GEMMA,
    LISBON,
    MCPARR,
    NOW,
    RULES,
    Clock,
    agent,
    contents,
    memory,
    owner_turn,
    reminder_tools,
    reminder_turn,
    resumed,
    tool_rows,
    two_brains,
    web_tools,
)
from tests.fakes import NoFacts
from tests.helpers import (
    DIOGO,
    FakeEmbedder,
    FakeModel,
    add_queue_item,
    calls,
    make_agent,
    make_config,
    text,
)
from tests.mcp.servers import COOKBOOK_INSTRUCTIONS, in_process, make_cookbook, make_mcparr

MR_FERREIRA = dataclasses.replace(DIOGO, address="Mr. Ferreira")

# --- prompt and context --------------------------------------------------------


async def test_default_prompt_and_context(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    await history.append("diogo", "user", "earlier")
    await history.append("diogo", "assistant", "earlier reply")
    model = FakeModel([text("Hello!")])
    result = await agent(sessions, model).run(await owner_turn(sessions, "hi"))
    assert result.reply == "Hello!" and not result.reset_requested
    messages = model.requests[0][0]
    assert messages[0] == {
        "role": "system",
        "content": _butler_persona("sir") + "\n\n" + RULES + "\n\n" + COOKBOOK_SECTION,
    }
    assert contents(messages[1:]) == ["earlier", "earlier reply", "hi"]


async def test_custom_persona(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, ai={"persona": "Be terse."}).run(await owner_turn(sessions, "hi"))
    assert model.requests[0][0][0]["content"] == (
        "Be terse.\n\n" + RULES + "\n\n" + COOKBOOK_SECTION
    )


async def test_custom_address(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model, users=[MR_FERREIRA]).run(
        await owner_turn(sessions, "hi", MR_FERREIRA)
    )
    assert model.requests[0][0][0]["content"].startswith(_butler_persona("Mr. Ferreira"))


async def test_context_window_limit(sessions: async_sessionmaker[AsyncSession]) -> None:
    history = History(sessions)
    for i in range(10):
        await history.append("diogo", "user", f"m{i}")
    model = FakeModel([text("ok")])
    await agent(sessions, model, history={"max_entries": 3}).run(await owner_turn(sessions, "last"))
    assert contents(model.requests[0][0][1:]) == ["m8", "m9", "last"]


async def test_ask_turn_context(sessions: async_sessionmaker[AsyncSession]) -> None:
    await History(sessions).append("diogo", "user", "earlier")
    item = await add_queue_item(sessions, "ask", "is this worth telling?", "disk-check")
    model = FakeModel([text(_NO_REPLY)])
    result = await agent(sessions, model).run(
        AskTurn("is this worth telling?", item, "disk-check", user=DIOGO)
    )
    assert result.reply == _NO_REPLY
    messages = model.requests[0][0]
    assert _NO_REPLY in messages[0]["content"]
    assert contents(messages[1:2]) == ["earlier"]
    assert messages[-1] == {
        "role": "user",
        "content": "[Mon 2 Nov 14:05] [ask:disk-check] is this worth telling?",
    }


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
        await agent(sessions, model, users=[MR_FERREIRA]).run(
            await owner_turn(sessions, "hi", MR_FERREIRA)
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
    model = FakeModel([text(_NO_REPLY)])
    await agent(sessions, model, now=JUST_AFTER_MIDNIGHT).run(
        AskTurn("x", item, "disk-check", user=DIOGO)
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


FELL_BACK = Fallback("bonsai", "gemma")


async def test_fallback_before_any_tool_runs_the_turn_again(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    brains = two_brains("bonsai")
    model = FakeModel([BAD_MODEL, text("gemma's answer")])
    with caplog.at_level(logging.WARNING):
        result = await agent(sessions, model, brains=brains).run(await owner_turn(sessions, "hi"))
    assert result.reply == "gemma's answer" and result.fallback == FELL_BACK
    assert model.models == ["bonsai:8b", GEMMA]
    assert brains.current.name == "gemma"
    retry = model.requests[1][0]
    assert brain_line("gemma", GEMMA, ["bonsai"], {_SET_MODEL}) in retry[0]["content"]
    assert contents(retry[1:]) == ["hi"]
    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "bonsai" in warning and "gemma" in warning and "does not support tools" in warning


async def test_fallback_after_a_tool_ran(sessions: async_sessionmaker[AsyncSession]) -> None:
    brains = two_brains("bonsai")
    model = FakeModel([calls(("mcparr_get_downloads", "{}")), BAD_MODEL])
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model, brains=brains).run(await owner_turn(sessions, "downloads?"))
    assert exc.value.notice == notices.failed("sir", True)
    assert exc.value.fallback == FELL_BACK
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
    assert exc.value.fallback == FELL_BACK
    assert model.models == ["bonsai:8b", GEMMA]


async def test_startup_brain_failure_has_no_fallback(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([BAD_MODEL])
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model, brains=two_brains()).run(await owner_turn(sessions, "hi"))
    assert exc.value.fallback is None and model.models == [GEMMA]


async def test_unreachable_server_does_not_fall_back(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    brains = two_brains("bonsai")
    model = FakeModel([ModelUnreachable("can't reach the model server (ConnectError)")])
    with pytest.raises(TurnFailed) as exc:
        await agent(sessions, model, brains=brains).run(await owner_turn(sessions, "hi"))
    assert exc.value.retryable and exc.value.fallback is None
    assert brains.current.name == "bonsai"


async def test_ask_turn_falls_back(sessions: async_sessionmaker[AsyncSession]) -> None:
    brains = two_brains("bonsai")
    item = await add_queue_item(sessions, "ask", "anything?", "disk-check")
    model = FakeModel([BAD_MODEL, text(_NO_REPLY)])
    result = await agent(sessions, model, brains=brains).run(
        AskTurn("anything?", item, "disk-check", user=DIOGO)
    )
    assert result.reply == _NO_REPLY and result.fallback == FELL_BACK
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
        _WEB_SEARCH,
        _WEB_FETCH,
        _END_CONVERSATION,
        _SEARCH_CONVERSATIONS,
        _RECALL_CONVERSATION,
        "alfred_remind_set",
        "alfred_reminders_list",
        "alfred_reminder_cancel",
        _SET_MODEL,
        _CALCULATE,
        _DATE,
        *MCP_TOOLS,
    ]


async def test_tool_order_on_ask_turns(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    model = FakeModel([text(_NO_REPLY)])
    item = await add_queue_item(sessions, "ask", "93%", "disk-check")
    await all_toolsets_agent(sessions, model, web_http).run(
        AskTurn("93%", item, "disk-check", user=DIOGO)
    )
    assert [t["function"]["name"] for t in model.requests[0][1]] == [
        _WEB_SEARCH,
        _WEB_FETCH,
        _CALCULATE,
        _DATE,
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
    a = Agent(
        make_config(),
        model,
        History(sessions),
        sessions,
        toolsets=[recorder],
        brains=brains,
        attachments=None,
        facts=NoFacts(),
    )
    await a.run(await owner_turn(sessions, "hi"))
    assert recorder.opened == 1  # the MCP servers are listed once per turn
    assert len(recorder.attempts) == 2
    assert recorder.attempts[0] is not recorder.attempts[1]
    assert [ctx.user_texts for ctx in recorder.attempts] == [["hi"], ["hi"]]


# --- message times (change add-utility-tools) --------------------------------------


async def test_ask_input_carries_the_time_now(sessions: async_sessionmaker[AsyncSession]) -> None:
    item = await add_queue_item(sessions, "ask", "93% full", "disk-check")
    model = FakeModel([text(_NO_REPLY)])
    at_three = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)  # 03:00 in Lisbon (WET)
    await agent(sessions, model, now=at_three).run(
        AskTurn("93% full", item, "disk-check", user=DIOGO)
    )
    assert model.requests[0][0][-1]["content"] == "[Tue 10 Nov 03:00] [ask:disk-check] 93% full"


async def test_owner_message_reaches_the_model_stamped(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    await agent(sessions, model).run(await owner_turn(sessions, "what time is it?"))
    latest = model.requests[0][0][-1]["content"]
    assert _STAMP.match(latest) and latest.endswith("] what time is it?")


async def test_echoed_stamp_is_removed(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([text("[Mon 2 Nov 14:06] It is just past two, sir.")])
    result = await agent(sessions, model).run(await owner_turn(sessions, "what time is it?"))
    assert result.reply == "It is just past two, sir."


async def test_echoed_stamp_before_no_reply(sessions: async_sessionmaker[AsyncSession]) -> None:
    item = await add_queue_item(sessions, "ask", "93% full", "disk-check")
    model = FakeModel([text(f"[Mon 2 Nov 14:05] {_NO_REPLY}")])
    result = await agent(sessions, model).run(AskTurn("93% full", item, "disk-check", user=DIOGO))
    assert is_no_reply(result.reply)


async def test_other_brackets_are_kept(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([text("Saved [draft] for later.")])
    result = await agent(sessions, model).run(await owner_turn(sessions, "save it"))
    assert result.reply == "Saved [draft] for later."


# --- images in tool results (change add-attachments, design D5) -----------------


class Camera(LocalToolset):
    """A tool whose result carries images."""

    definitions = [
        {"type": "function", "function": {"name": "snap", "parameters": {"type": "object"}}}
    ]

    def __init__(self, count: int = 1) -> None:
        self.count = count

    async def call(self, name: str, raw_arguments: object, attempt: AttemptContext) -> ToolOutcome:
        images = tuple(ModelImage(b"img%d" % i, "image/png") for i in range(self.count))
        return ToolOutcome(True, "Here it is.", images)


@pytest.mark.parametrize(("count", "note"), [(1, "[1 image]"), (2, "[2 images]")])
async def test_tool_result_with_images(
    sessions: async_sessionmaker[AsyncSession], count: int, note: str
) -> None:
    model = FakeModel([calls(("snap", "{}")), text("A red square, sir.")])
    a = Agent(
        make_config(),
        model,
        History(sessions),
        sessions,
        toolsets=[Camera(count)],
        brains=Brains.from_config(make_config().ai),
        attachments=None,
        facts=NoFacts(),
    )
    await a.run(await owner_turn(sessions, "look"))
    result = model.requests[1][0][-1]
    assert result["role"] == "tool" and result["tool_call_id"] == "c0"
    assert result["content"][0] == {"type": "text", "text": "Here it is."}
    assert [p["image_url"]["url"] for p in result["content"][1:]] == [
        f"data:image/png;base64,{base64.b64encode(b'img%d' % i).decode()}" for i in range(count)
    ]
    (row,) = await tool_rows(sessions)
    assert row.result_excerpt == f"Here it is. {note}"
    assert "base64" not in row.result_excerpt


async def test_tool_result_without_images_stays_text(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([calls(("snap", "{}")), text("Nothing there.")])
    a = Agent(
        make_config(),
        model,
        History(sessions),
        sessions,
        toolsets=[Camera(0)],
        brains=Brains.from_config(make_config().ai),
        attachments=None,
        facts=NoFacts(),
    )
    await a.run(await owner_turn(sessions, "look"))
    assert model.requests[1][0][-1]["content"] == "Here it is."
    assert (await tool_rows(sessions))[0].result_excerpt == "Here it is."


# --- Repeating reminder turns (change add-recurring-reminders, design D6) ---------------


async def test_repeating_reminder_input(sessions: async_sessionmaker[AsyncSession]) -> None:
    tuesday = datetime(2026, 11, 17, 8, 0, tzinfo=LISBON)
    model = FakeModel([text("Good morning, sir. Sunny, 18 degrees.")])
    turn = await reminder_turn(
        sessions, 31, "brief me", due_at=tuesday, repeat_label="every weekday at 08:00"
    )
    await agent(sessions, model, now=tuesday).run(turn)
    assert model.requests[0][0][-1]["content"] == (
        "[reminder:#31 due Tuesday 17 November 2026, 08:00; now 08:00; every weekday at 08:00]"
        " brief me"
    )


async def test_one_shot_reminder_input_unchanged(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("Your call to mum is due, sir.")])
    await agent(sessions, model, now=DUE).run(await reminder_turn(sessions))
    assert model.requests[0][0][-1]["content"] == (
        "[reminder:#12 due Monday 9 November 2026, 17:00; now 17:00] call mum"
    )


async def test_quiet_reminder_turn_is_told_about_no_reply(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    loud, quiet = FakeModel([text("ok")]), FakeModel([text("ok")])
    await agent(sessions, loud, now=DUE).run(await reminder_turn(sessions))
    await agent(sessions, quiet, now=DUE).run(await reminder_turn(sessions, quiet_ok=True))
    assert _NO_REPLY not in loud.requests[0][0][0]["content"]
    assert _NO_REPLY in quiet.requests[0][0][0]["content"]


# --- waiting for the user's answer (change add-user-input-requests) ------------------

CONFIRMS_ADD = dataclasses.replace(DIOGO, confirm=AllowList.of("mcparr_add_media"))
FIND = ("mcparr_get_downloads", "{}")
ADD = ("mcparr_add_media", '{"title": "Dune"}')
REMIND = (_REMIND_SET, '{"text": "watch Dune", "in_minutes": 60}')


def confirming(sessions: async_sessionmaker[AsyncSession], model: FakeModel, **kw: object) -> Agent:
    return agent(sessions, model, reminders=reminder_tools(sessions, Clock(NOW)), **kw)


async def parked_turn(
    sessions: async_sessionmaker[AsyncSession], model: FakeModel
) -> tuple[Agent, TurnInput, TurnResult]:
    """A turn whose user confirms mcparr_add_media; the model's script decides the calls."""
    alfred = confirming(sessions, model)
    turn = await owner_turn(sessions, "add Dune and remind me", CONFIRMS_ADD)
    return alfred, turn, await alfred.run(turn)


async def test_turn_parks_at_a_listed_call(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([calls(FIND, ADD, REMIND)])
    _, _, result = await parked_turn(sessions, model)
    waiting = result.waiting
    assert waiting is not None and result.reply == ""
    assert (waiting.checkpoint.call.name, waiting.checkpoint.call_index) == ("mcparr_add_media", 1)
    assert [c.name for c in waiting.checkpoint.remaining] == [_REMIND_SET]
    # The tail: the assistant message with the three calls, and find's result.
    assert [m["role"] for m in waiting.checkpoint.tail] == ["assistant", "tool"]
    assert len(waiting.checkpoint.tail[0]["tool_calls"]) == 3
    assert "Dune" in waiting.checkpoint.tail[1]["content"]
    assert (waiting.checkpoint.requests_used, waiting.checkpoint.seq) == (1, 2)
    assert (
        waiting.checkpoint.upto_entry_id is not None
        and waiting.checkpoint.conversation_id is not None
    )
    # find ran, the park is recorded, remind didn't run.
    rows = await tool_rows(sessions)
    assert [(r.tool, r.ok, r.result_excerpt) for r in rows][1:] == [
        ("mcparr_add_media", False, "waiting for the user's answer")
    ]
    assert rows[0].tool == "mcparr_get_downloads" and rows[0].ok
    assert await Reminders(sessions).pending("diogo") == []


async def test_do_it_runs_the_call_and_the_turn_goes_on(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([calls(FIND, ADD, REMIND), text("Added Dune; I'll remind you.")])
    alfred, turn, parked = await parked_turn(sessions, model)
    result = await alfred.run(resumed(turn, parked, True))
    assert result.reply == "Added Dune; I'll remind you."
    assert result.side_effects_possible and result.waiting is None
    rows = await tool_rows(sessions)
    assert [(r.seq, r.tool, r.ok) for r in rows] == [
        (1, "mcparr_get_downloads", True),
        (2, "mcparr_add_media", False),
        (3, "mcparr_add_media", True),
        (4, _REMIND_SET, True),
    ]
    # The model saw add's real result, then remind's, after the same assistant message.
    messages = model.requests[1][0]
    assert [m["role"] for m in messages[-4:]] == ["assistant", "tool", "tool", "tool"]
    assert "added Dune" in messages[-2]["content"]
    assert [m.get("tool_call_id") for m in messages[-3:]] == ["c0", "c1", "c2"]
    assert len(await Reminders(sessions).pending("diogo")) == 1


async def test_cancel_declines_without_running(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([calls(ADD), text("As you wish, I won't add it.")])
    alfred, turn, parked = await parked_turn(sessions, model)
    result = await alfred.run(resumed(turn, parked, False))
    assert result.reply == "As you wish, I won't add it."
    assert model.requests[1][0][-1]["content"] == _DECLINED
    rows = await tool_rows(sessions)
    assert [(r.tool, r.ok, r.result_excerpt) for r in rows][-1] == (
        "mcparr_add_media",
        False,
        _DECLINED,
    )


async def test_two_listed_calls_ask_one_after_the_other(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    alien = ("mcparr_add_media", '{"title": "Alien"}')
    model = FakeModel([calls(ADD, alien), text("Both added.")])
    alfred, turn, first = await parked_turn(sessions, model)
    assert first.waiting is not None and first.waiting.checkpoint.call.arguments == ADD[1]
    second = await alfred.run(resumed(turn, first, True))
    assert second.waiting is not None
    assert (second.waiting.checkpoint.call.arguments, second.waiting.checkpoint.call_index) == (
        alien[1],
        1,
    )
    assert second.waiting.checkpoint.remaining == ()
    # Dune's real result is in the tail now.
    assert "added Dune" in second.waiting.checkpoint.tail[-1]["content"]
    done = await alfred.run(resumed(turn, second, True))
    assert done.reply == "Both added."
    assert len(model.requests) == 2


async def test_max_requests_counts_the_whole_turn(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([calls(FIND), calls(ADD), calls(FIND)])
    alfred = confirming(sessions, model, ai={"max_requests_per_turn": 3})
    turn = await owner_turn(sessions, "add Dune", CONFIRMS_ADD)
    parked = await alfred.run(turn)
    assert parked.waiting is not None and parked.waiting.checkpoint.requests_used == 2
    with pytest.raises(TurnFailed) as failed:
        await alfred.run(resumed(turn, parked, True))
    assert "stopped after 3 model requests" in failed.value.reason
    assert len(model.requests) == 3


async def test_ai_down_after_the_answer_is_not_retried(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([calls(ADD), ModelUnreachable("ai.lan timed out")])
    alfred, turn, parked = await parked_turn(sessions, model)
    with pytest.raises(TurnFailed) as failed:
        await alfred.run(resumed(turn, parked, True))
    assert not failed.value.retryable and failed.value.side_effects_possible
    assert failed.value.notice == notices.unreachable_mid("sir")


async def test_entries_recorded_while_waiting_are_not_in_the_resumed_context(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([calls(ADD), text("Done.")])
    alfred, turn, parked = await parked_turn(sessions, model)
    history = History(sessions)
    await history.append("diogo", "ask_prompt", "anything new?", source="jellyfin")
    await history.append("diogo", "ask_reply", "Nothing.", source="jellyfin")
    await alfred.run(resumed(turn, parked, True))
    resumed_context = contents([m for m in model.requests[1][0] if m["role"] != "tool"][1:])
    assert not any("anything new?" in c or "Nothing." in c for c in resumed_context if c)
    assert any("add Dune and remind me" in c for c in resumed_context if c)


async def test_images_in_the_tail_are_replaced_by_a_note(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    class Pictures(LocalToolset):
        definitions = [{"type": "function", "function": {"name": "alfred_look", "parameters": {}}}]

        async def call(
            self, name: str, raw_arguments: object, attempt: AttemptContext
        ) -> ToolOutcome:
            image = ModelImage(b"png", "image/png")
            return ToolOutcome(True, "two pictures", images=(image, image))

    config = make_config(mcp_servers=[MCPARR])
    alfred = Agent(
        config,
        FakeModel([calls(("alfred_look", "{}"), ADD)]),
        History(sessions),
        sessions,
        toolsets=[
            Pictures(),
            *make_agent(
                config,
                FakeModel(),
                History(sessions),
                sessions,
                in_process({"mcparr": make_mcparr()}),
            )._toolsets,
        ],
        now=lambda: NOW,
        brains=Brains.from_config(config.ai),
        attachments=None,
        facts=NoFacts(),
    )
    turn = await owner_turn(sessions, "look, then add", CONFIRMS_ADD)
    result = await alfred.run(turn)
    assert result.waiting is not None
    assert (
        result.waiting.checkpoint.tail[1]["content"]
        == "two pictures\n[2 images not kept while waiting]"
    )
