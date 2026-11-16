"""The MCP servers as one toolset: routing, exclusions, groups and prompt notes."""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from mcp import Client
from mcp.server.request_state import RequestStateSecurity
from pydantic import SecretStr

from alfred.config import McpServer, User
from alfred.mcp.client import Connector
from alfred.mcp.registry import ServerInstructions, late_answer
from alfred.mcp.toolset import McpTools, OpenMcpTools, server_instructions, unavailable_note
from alfred.tools.base import (
    AllowList,
    AttemptContext,
    Gated,
    PromptNote,
    ToolOutcome,
    TurnInput,
    dispatch,
)
from tests.helpers import ANA, DIOGO, make_config
from tests.mcp.servers import (
    COOKBOOK_INSTRUCTIONS,
    config,
    in_process,
    make_asking,
    make_cookbook,
    make_mcparr,
)

MCPARR = config("mcparr")
COOKBOOK = config("cookbook", frozenset({"delete_recipe"}))
TURN = TurnInput("chat", "hi", 1, user=DIOGO)


def mcp_tools(**servers: object) -> McpTools:
    connect = in_process(servers)  # type: ignore[arg-type]
    return McpTools(
        lambda user: [MCPARR, COOKBOOK],
        connect,
        list_timeout_s=5,
        call_timeout_s=5,
        instructions_max_chars=2000,
        max_chars=4000,
    )


async def test_routes_exclusions_and_groups() -> None:
    opened = await mcp_tools(mcparr=make_mcparr(), cookbook=make_cookbook()).open(TURN)
    assert opened.handles("mcparr_get_downloads")
    assert opened.handles("cookbook_delete_recipe")  # excluded, refused by the toolset
    assert not opened.handles("alfred_set_model")
    assert opened.group_of("mcparr_get_downloads") == "mcparr"
    assert opened.group_of("cookbook_get_recipe") == "cookbook"

    ctx = AttemptContext(TURN)
    outcome = await dispatch([opened], "mcparr_get_downloads", "{}", ctx)
    assert json.loads(outcome.text) == {"items": [{"title": "Dune", "progress": 0.5}]}
    refused = await dispatch([opened], "cookbook_delete_recipe", "{}", ctx)
    assert (
        refused.text
        == "Error: tool 'cookbook_delete_recipe' is disabled in alfred's configuration."
    )


async def test_prompt_notes() -> None:
    opened = await mcp_tools(mcparr=ConnectionError("down"), cookbook=make_cookbook()).open(TURN)
    assert opened.prompt_notes == [
        PromptNote(
            server_instructions(ServerInstructions("cookbook", COOKBOOK_INSTRUCTIONS)),
            "cookbook",
            len(COOKBOOK_INSTRUCTIONS),
        ),
        PromptNote(unavailable_note(["mcparr"])),
    ]


async def test_no_notes_when_nothing_to_say() -> None:
    opened = await mcp_tools(mcparr=make_mcparr(), cookbook=make_cookbook(None)).open(TURN)
    assert opened.prompt_notes == []


# --- Per user (change add-multiple-users) ----------------------------------------------

ANA_TOKEN = SecretStr("tok-mcparr-ana")
ANA_WITH_TOKEN = dataclasses.replace(ANA, mcp_tokens={"mcparr": ANA_TOKEN})


def recording(servers: dict[str, object], seen: list[tuple[str, str]]) -> Connector:
    """The in-process connector, recording each connection's server and token."""
    inner = in_process(servers)  # type: ignore[arg-type]

    @asynccontextmanager
    async def connect(server: McpServer, http_timeout_s: float) -> AsyncIterator[Client]:
        seen.append((server.name, server.token.get_secret_value()))
        async with inner(server, http_timeout_s) as client:
            yield client

    return connect


def per_user(connect: Connector, *users: User) -> McpTools:
    cfg = make_config(mcp_servers=[MCPARR, COOKBOOK], users=users or (DIOGO, ANA_WITH_TOKEN))
    return McpTools.from_config(cfg, connect)


async def test_per_user_token() -> None:
    seen: list[tuple[str, str]] = []
    tools = per_user(recording({"mcparr": make_mcparr(), "cookbook": make_cookbook()}, seen))
    ana = TurnInput("chat", "hi", 2, user=ANA_WITH_TOKEN)
    opened = await tools.open(ana)
    listed = len(seen)
    outcome = await dispatch([opened], "mcparr_add_media", '{"title": "Dune"}', AttemptContext(ana))
    assert outcome.ok
    assert seen[listed:] == [("mcparr", "tok-mcparr-ana")]  # the call
    assert sorted(seen[:listed]) == [("cookbook", "tok-cookbook"), ("mcparr", "tok-mcparr-ana")]

    seen.clear()
    opened = await tools.open(TURN)
    await dispatch([opened], "mcparr_get_downloads", "{}", AttemptContext(TURN))
    assert seen and all(token == f"tok-{name}" for name, token in seen)


async def test_server_outside_the_allow_list_is_not_contacted() -> None:
    seen: list[tuple[str, str]] = []
    no_cookbook = dataclasses.replace(ANA, tools=AllowList.of("alfred_*", "mcparr_find_media"))
    servers = {"mcparr": make_mcparr(), "cookbook": ConnectionError("down")}
    tools = per_user(recording(servers, seen), DIOGO, no_cookbook)
    opened = await tools.open(TurnInput("chat", "hi", 2, user=no_cookbook))
    assert [name for name, _ in seen] == ["mcparr"]
    # Not listed, so not reported as unavailable either; no cookbook instructions.
    assert opened.prompt_notes == []
    assert not any(n.startswith("cookbook_") for n in opened.tools.routes)


async def test_empty_allow_list_contacts_no_server() -> None:
    seen: list[tuple[str, str]] = []
    nothing = dataclasses.replace(ANA, tools=AllowList())
    tools = per_user(recording({"mcparr": make_mcparr(), "cookbook": make_cookbook()}, seen))
    opened = await tools.open(TurnInput("chat", "hi", 2, user=nothing))
    assert seen == [] and opened.definitions == [] and opened.prompt_notes == []


async def test_instructions_dropped_without_an_allowed_tool(
    caplog: pytest.LogCaptureFixture,
) -> None:
    typo = dataclasses.replace(ANA, tools=AllowList.of("cookbook_get_recipee"))
    tools = per_user(in_process({"mcparr": make_mcparr(), "cookbook": make_cookbook()}))
    with caplog.at_level(logging.WARNING, logger="alfred.mcp.toolset"):
        opened = await tools.open(TurnInput("chat", "hi", 2, user=typo))
    assert opened.prompt_notes == []
    gated = Gated(opened, typo.tools)
    assert gated.definitions == []
    (record,) = caplog.records
    message = record.getMessage()
    assert "ana" in message and "cookbook" in message and "'cookbook_get_recipee'" in message


async def test_instructions_kept_with_an_allowed_tool() -> None:
    one = dataclasses.replace(ANA, tools=AllowList.of("cookbook_get_recipe"))
    tools = per_user(in_process({"mcparr": make_mcparr(), "cookbook": make_cookbook()}))
    opened = await tools.open(TurnInput("chat", "hi", 2, user=one))
    assert [n.log_label for n in opened.prompt_notes] == ["cookbook"]
    assert [d["function"]["name"] for d in Gated(opened, one.tools).definitions] == [
        "cookbook_get_recipe"
    ]


async def test_hidden_tool_call_never_reaches_the_server() -> None:
    seen: list[tuple[str, str]] = []
    tools = per_user(recording({"mcparr": make_mcparr(), "cookbook": make_cookbook()}, seen))
    ana = TurnInput("chat", "hi", 2, user=ANA_WITH_TOKEN)
    opened = Gated(await tools.open(ana), ANA_WITH_TOKEN.tools)
    seen.clear()
    outcome = await dispatch([opened], "mcparr_get_downloads", "{}", AttemptContext(ana))
    assert outcome.text == "Error: unknown tool 'mcparr_get_downloads'."
    assert seen == []


# --- Resuming a server's question (change add-user-input-requests) -------------------

KEY = b"0123456789abcdef0123456789abcdef"


def asking_tools(servers: dict[str, object]) -> McpTools:
    """McpTools over ``servers``, a dict a test may change between the legs."""
    return McpTools(
        lambda user: [MCPARR],
        in_process(servers),  # type: ignore[arg-type]
        list_timeout_s=5,
        call_timeout_s=5,
        instructions_max_chars=2000,
        max_chars=4000,
    )


async def first_leg(
    servers: dict[str, object], name: str, arguments: str
) -> tuple[OpenMcpTools, ToolOutcome]:
    opened = await asking_tools(servers).open(TURN)
    outcome = await opened.call(name, arguments, AttemptContext(TURN))
    assert outcome.need is not None, outcome
    return opened, outcome


def answer_of(outcome: ToolOutcome, label: str) -> object:
    assert outcome.need is not None
    return next(o.answer for o in outcome.need.question.options if o.label == label)


async def resume(
    servers: dict[str, object], name: str, arguments: str, need: ToolOutcome, label: str
) -> ToolOutcome:
    """The resume as a resumed turn does it: the toolset opened anew."""
    assert need.need is not None
    opened = await asking_tools(servers).open(TURN)
    return await opened.resume(
        name, arguments, need.need.state, answer_of(need, label), AttemptContext(TURN)
    )


async def test_accept_reaches_the_tool() -> None:
    ran: list[str] = []
    servers: dict[str, object] = {"mcparr": make_asking(ran=ran)}
    _, need = await first_leg(servers, "mcparr_add_media", '{"title": "Dune"}')
    outcome = await resume(servers, "mcparr_add_media", '{"title": "Dune"}', need, "Yes")
    assert outcome.ok and "added Dune" in outcome.text
    assert ran == ["add_media"]


async def test_decline_reaches_the_tool() -> None:
    servers: dict[str, object] = {"mcparr": make_asking()}
    _, need = await first_leg(servers, "mcparr_add_media", '{"title": "Dune"}')
    outcome = await resume(servers, "mcparr_add_media", '{"title": "Dune"}', need, "No")
    assert outcome.ok and "not added (decline)" in outcome.text


async def test_a_second_question_is_a_new_need() -> None:
    servers: dict[str, object] = {"mcparr": make_asking()}
    args = '{"title": "Dune"}'
    _, first = await first_leg(servers, "mcparr_add_in_quality", args)
    second = await resume(servers, "mcparr_add_in_quality", args, first, "Yes")
    assert second.need is not None
    assert second.need.question.body == "mcparr asks: Which quality?"
    assert second.need.state["round"] == 2
    done = await resume(servers, "mcparr_add_in_quality", args, second, "4K")
    assert done.ok and "added Dune in quality='4K'" in done.text


async def test_server_with_a_new_key_gives_the_late_answer() -> None:
    ran: list[str] = []
    servers: dict[str, object] = {"mcparr": make_asking()}
    _, need = await first_leg(servers, "mcparr_add_media", '{"title": "Dune"}')
    servers["mcparr"] = make_asking(ran=ran)  # restarted: a new process-local key
    outcome = await resume(servers, "mcparr_add_media", '{"title": "Dune"}', need, "Yes")
    assert outcome == ToolOutcome(False, late_answer("mcparr", "mcparr_add_media"))
    assert ran == []


async def test_changed_arguments_give_the_late_answer() -> None:
    ran: list[str] = []
    servers: dict[str, object] = {"mcparr": make_asking(ran=ran)}
    _, need = await first_leg(servers, "mcparr_add_media", '{"title": "Dune"}')
    outcome = await resume(servers, "mcparr_add_media", '{"title": "Alien"}', need, "Yes")
    assert outcome == ToolOutcome(False, late_answer("mcparr", "mcparr_add_media"))
    assert ran == []


async def test_shared_key_survives_a_server_restart() -> None:
    ran: list[str] = []
    servers: dict[str, object] = {"mcparr": make_asking(RequestStateSecurity(keys=[KEY]))}
    _, need = await first_leg(servers, "mcparr_add_media", '{"title": "Dune"}')
    servers["mcparr"] = make_asking(RequestStateSecurity(keys=[KEY]), ran=ran)
    outcome = await resume(servers, "mcparr_add_media", '{"title": "Dune"}', need, "Yes")
    assert outcome.ok and "added Dune" in outcome.text
    assert ran == ["add_media"]


async def test_resume_without_mcp_state_is_refused() -> None:
    servers: dict[str, object] = {"mcparr": make_asking()}
    opened = await asking_tools(servers).open(TURN)
    outcome = await opened.resume(
        "mcparr_add_media", "{}", {"via": "confirm"}, True, AttemptContext(TURN)
    )
    assert not outcome.ok


async def test_too_many_rounds_is_an_error() -> None:
    servers: dict[str, object] = {"mcparr": make_asking()}
    args = '{"title": "Dune"}'
    _, first = await first_leg(servers, "mcparr_add_in_quality", args)
    assert first.need is not None
    tired = dataclasses.replace(
        first, need=dataclasses.replace(first.need, state={**first.need.state, "round": 10})
    )
    outcome = await resume(servers, "mcparr_add_in_quality", args, tired, "Yes")
    assert outcome == ToolOutcome(
        False, "Error: mcparr kept asking for input (10 questions); the call did not complete."
    )
