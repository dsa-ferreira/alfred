from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from mcp import Client
from mcp.types import (
    CreateMessageRequest,
    CreateMessageRequestParams,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitRequestURLParams,
    ElicitResult,
    InputRequiredResult,
    SamplingMessage,
    TextContent,
)

from alfred.features.mcp.config import McpServer
from alfred.features.mcp.registry import _classify, _tool_name, build_turn_tools, call_tool
from alfred.kernel.toolset import ToolOutcome
from tests.features.mcp.servers import (
    COOKBOOK_INSTRUCTIONS,
    config,
    counting,
    in_process,
    make_asking,
    make_cookbook,
    make_mcparr,
)

MCPARR = config("mcparr")
COOKBOOK = config("cookbook", frozenset({"delete_recipe"}))


def names(definitions: list[dict]) -> set[str]:
    return {d["function"]["name"] for d in definitions}


# --- Server-prefixed tool names ---------------------------------------------------


async def test_prefixed_names_and_routing() -> None:
    connect = in_process({"mcparr": make_mcparr(), "cookbook": make_cookbook()})
    turn = await build_turn_tools([MCPARR, COOKBOOK], connect, 5)
    assert "mcparr_get_downloads" in names(turn.definitions)
    route = turn.routes["mcparr_get_downloads"]
    assert (route.server.name, route.tool) == ("mcparr", "get_downloads")

    outcome = await call_tool(
        turn, "mcparr_get_downloads", "{}", connect=connect, call_timeout_s=5, max_chars=4000
    )
    assert outcome.ok
    assert json.loads(outcome.text) == {"items": [{"title": "Dune", "progress": 0.5}]}


async def test_same_tool_on_two_servers() -> None:
    connect = in_process({"mcparr": make_mcparr(), "cookbook": make_cookbook()})
    turn = await build_turn_tools([MCPARR, COOKBOOK], connect, 5)
    assert {"mcparr_check_health", "cookbook_check_health"} <= names(turn.definitions)
    for server in ("mcparr", "cookbook"):
        outcome = await call_tool(
            turn, f"{server}_check_health", None, connect=connect, call_timeout_s=5, max_chars=99
        )
        # SDK v2 wraps a plain return value as structured content {"result": ...}.
        assert json.loads(outcome.text) == {"result": f"{server} ok"}


def test_name_cleaning() -> None:
    assert _tool_name("my-server", "get.things now") == "my-server_get_things_now"


async def test_definition_shape() -> None:
    connect = in_process({"mcparr": make_mcparr()})
    turn = await build_turn_tools([MCPARR], connect, 5)
    add = next(d for d in turn.definitions if d["function"]["name"] == "mcparr_add_media")
    assert add["type"] == "function"
    assert add["function"]["description"] == "Add a movie or series."
    assert add["function"]["parameters"]["properties"]["title"]["type"] == "string"


async def test_collisions_and_long_names_are_skipped(caplog: pytest.LogCaptureFixture) -> None:
    from mcp.server import MCPServer

    a = MCPServer("a")

    @a.tool(name="x.y")
    def dotted() -> str:
        return "dotted"

    @a.tool(name="x_y")
    def underscored() -> str:
        return "underscored"

    @a.tool(name="n" * 70)
    def long() -> str:
        return "long"

    connect = in_process({"a": a})
    with caplog.at_level(logging.WARNING):
        turn = await build_turn_tools([config("a")], connect, 5)
    assert names(turn.definitions) == {"a_x_y"}
    assert "collides" in caplog.text and "too long" in caplog.text


# --- Excluded tools ------------------------------------------------------------


async def test_excluded_tool_hidden_and_refused() -> None:
    connect = in_process({"cookbook": make_cookbook()})
    turn = await build_turn_tools([COOKBOOK], connect, 5)
    assert "cookbook_delete_recipe" not in names(turn.definitions)
    # The server's delete_recipe raises AssertionError if it is ever reached.
    outcome = await call_tool(
        turn, "cookbook_delete_recipe", '{"recipe_id": 1}', connect=connect, call_timeout_s=5,
        max_chars=4000,
    )  # fmt: skip
    assert not outcome.ok
    assert "disabled" in outcome.text


async def test_excluded_tool_refused_while_server_down() -> None:
    connect = in_process({"cookbook": ConnectionError("down")})
    turn = await build_turn_tools([COOKBOOK], connect, 5)
    outcome = await call_tool(
        turn, "cookbook_delete_recipe", "{}", connect=connect, call_timeout_s=5, max_chars=99
    )
    assert "disabled" in outcome.text


async def test_unknown_excluded_name_logged(caplog: pytest.LogCaptureFixture) -> None:
    connect = in_process({"cookbook": make_cookbook()})
    server = config("cookbook", frozenset({"delete_recipes"}))
    with caplog.at_level(logging.WARNING):
        await build_turn_tools([server], connect, 5)
    assert "cookbook" in caplog.text and "delete_recipes" in caplog.text


# --- Unavailable servers --------------------------------------------------------


async def test_one_server_down() -> None:
    connect = in_process({"mcparr": ConnectionError("refused"), "cookbook": make_cookbook()})
    turn = await build_turn_tools([MCPARR, COOKBOOK], connect, 5)
    assert turn.unavailable == ["mcparr"]
    offered = names(turn.definitions)
    assert offered and all(n.startswith("cookbook_") for n in offered)


async def test_slow_server_times_out() -> None:
    @asynccontextmanager
    async def hang(server: McpServer, http_timeout_s: float) -> AsyncIterator[Client]:
        await asyncio.sleep(60)
        yield  # type: ignore[misc]

    turn = await build_turn_tools([MCPARR], hang, 0.05)
    assert turn.unavailable == ["mcparr"]


async def test_server_comes_back() -> None:
    servers: dict = {"mcparr": ConnectionError("refused")}
    connect = in_process(servers)
    assert (await build_turn_tools([MCPARR], connect, 5)).unavailable == ["mcparr"]
    servers["mcparr"] = make_mcparr()
    turn = await build_turn_tools([MCPARR], connect, 5)
    assert turn.unavailable == []
    assert "mcparr_get_downloads" in names(turn.definitions)


# --- Server instructions -------------------------------------------------------


async def test_instructions_collected() -> None:
    connect = in_process({"mcparr": make_mcparr(), "cookbook": make_cookbook()})
    turn = await build_turn_tools([MCPARR, COOKBOOK], connect, 5)
    # mcparr sends none, so only cookbook contributes.
    assert [(i.server, i.text) for i in turn.instructions] == [("cookbook", COOKBOOK_INSTRUCTIONS)]


async def test_instructions_in_configuration_order() -> None:
    from mcp.server import MCPServer

    connect = in_process({"a": MCPServer("a", instructions="A"), "cookbook": make_cookbook()})
    turn = await build_turn_tools([COOKBOOK, config("a")], connect, 5)
    assert [i.server for i in turn.instructions] == ["cookbook", "a"]


async def test_no_instructions_when_server_down() -> None:
    connect = in_process({"cookbook": ConnectionError("down")})
    turn = await build_turn_tools([COOKBOOK], connect, 5)
    assert turn.unavailable == ["cookbook"]
    assert turn.instructions == []


@pytest.mark.parametrize("text", [None, "", "  \n\t "])
async def test_no_or_blank_instructions(text: str | None) -> None:
    connect = in_process({"cookbook": make_cookbook(text)})
    turn = await build_turn_tools([COOKBOOK], connect, 5)
    assert turn.instructions == []
    assert "cookbook_get_recipe" in names(turn.definitions)


async def test_muted_instructions_keep_tools() -> None:
    connect = in_process({"cookbook": make_cookbook()})
    muted = config("cookbook", frozenset({"delete_recipe"}), use_instructions=False)
    turn = await build_turn_tools([muted], connect, 5)
    assert turn.instructions == []
    assert "cookbook_get_recipe" in names(turn.definitions)


async def test_short_instructions_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    connect = in_process({"cookbook": make_cookbook("x" * 700)})
    with caplog.at_level(logging.WARNING):
        turn = await build_turn_tools([COOKBOOK], connect, 5, instructions_max_chars=2000)
    assert turn.instructions[0].text == "x" * 700
    assert "instructions" not in caplog.text


async def test_long_instructions_truncated(caplog: pytest.LogCaptureFixture) -> None:
    connect = in_process({"cookbook": make_cookbook("x" * 5000)})
    with caplog.at_level(logging.WARNING):
        turn = await build_turn_tools([COOKBOOK], connect, 5, instructions_max_chars=2000)
    assert turn.instructions[0].text == "x" * 2000 + "\n[truncated: 3000 more characters]"
    assert "cookbook" in caplog.text and "instructions" in caplog.text


async def test_changed_instructions_used_next_turn() -> None:
    servers: dict = {"cookbook": make_cookbook("old text")}
    connect = in_process(servers)
    first = await build_turn_tools([COOKBOOK], connect, 5)
    assert first.instructions[0].text == "old text"
    servers["cookbook"] = make_cookbook("new text")
    second = await build_turn_tools([COOKBOOK], connect, 5)
    assert second.instructions[0].text == "new text"


# --- Tool call routing ----------------------------------------------------------


async def test_unknown_tool() -> None:
    connect = in_process({"mcparr": make_mcparr()})
    turn = await build_turn_tools([MCPARR], connect, 5)
    outcome = await call_tool(
        turn, "mcparr_nope", "{}", connect=connect, call_timeout_s=5, max_chars=99
    )
    assert not outcome.ok and "unknown tool" in outcome.text


async def test_tool_error_becomes_result() -> None:
    connect = in_process({"cookbook": make_cookbook()})
    turn = await build_turn_tools([COOKBOOK], connect, 5)
    outcome = await call_tool(
        turn, "cookbook_get_recipe", '{"recipe_id": 7}', connect=connect, call_timeout_s=5,
        max_chars=4000,
    )  # fmt: skip
    assert not outcome.ok
    assert "recipe 7 does not exist" in outcome.text


async def test_server_error_becomes_result() -> None:
    servers: dict = {"mcparr": make_mcparr()}
    connect = in_process(servers)
    turn = await build_turn_tools([MCPARR], connect, 5)
    servers["mcparr"] = ConnectionError("connection refused")
    outcome = await call_tool(
        turn, "mcparr_check_health", "{}", connect=connect, call_timeout_s=5, max_chars=99
    )
    assert not outcome.ok
    assert "connection refused" in outcome.text


async def test_invalid_arguments() -> None:
    connect = in_process({"mcparr": make_mcparr()})
    turn = await build_turn_tools([MCPARR], connect, 5)
    bad_json = await call_tool(
        turn, "mcparr_add_media", "{nope", connect=connect, call_timeout_s=5, max_chars=99
    )
    assert not bad_json.ok and "not valid JSON" in bad_json.text
    missing = await call_tool(
        turn, "mcparr_add_media", "{}", connect=connect, call_timeout_s=5, max_chars=999
    )
    assert not missing.ok


async def test_call_timeout() -> None:
    connect = in_process({"mcparr": make_mcparr()})
    turn = await build_turn_tools([MCPARR], connect, 5)

    @asynccontextmanager
    async def hang(server: McpServer, http_timeout_s: float) -> AsyncIterator[Client]:
        await asyncio.sleep(60)
        yield  # type: ignore[misc]

    outcome = await call_tool(
        turn, "mcparr_check_health", "{}", connect=hang, call_timeout_s=0.05, max_chars=99
    )
    assert not outcome.ok and "timed out" in outcome.text


async def test_truncated_result() -> None:
    connect = in_process({"cookbook": make_cookbook()})
    turn = await build_turn_tools([COOKBOOK], connect, 5)
    outcome = await call_tool(
        turn, "cookbook_big", "{}", connect=connect, call_timeout_s=5, max_chars=100
    )
    assert outcome.ok
    full = json.dumps({"result": "x" * 10_000})
    assert outcome.text == full[:100] + f"\n[truncated: {len(full) - 100} more characters]"


# --- Server questions (change add-user-input-requests) -------------------------------


async def ask(tool: str, arguments: str = "{}") -> tuple[ToolOutcome, list[str], list[str]]:
    """Call an asking mcparr tool: the outcome, the connections opened, the bodies that ran."""
    ran: list[str] = []
    opened: list[str] = []
    connect = counting(in_process({"mcparr": make_asking(ran=ran)}), opened)
    turn = await build_turn_tools([MCPARR], connect, 5)
    opened.clear()
    outcome = await call_tool(
        turn, f"mcparr_{tool}", arguments, connect=connect, call_timeout_s=5, max_chars=999
    )
    return outcome, opened, ran


def buttons(outcome: ToolOutcome) -> list[tuple[str, object]]:
    assert outcome.need is not None, outcome
    return [(o.label, o.answer) for o in outcome.need.question.options]


async def test_empty_form_gives_yes_and_no() -> None:
    outcome, _, ran = await ask("add_media", '{"title": "Dune"}')
    assert buttons(outcome) == [
        ("Yes", {"action": "accept", "content": {}}),
        ("No", {"action": "decline"}),
    ]
    assert outcome.need is not None
    question, state = outcome.need.question, outcome.need.state
    assert question.body == "mcparr asks: Add 'Dune' to the library?"
    assert question.detail == "mcparr_add_media"
    assert state["via"] == "mcp" and state["round"] == 1
    assert state["key"] == "tests.features.mcp.servers:confirm_add" and state["request_state"]
    assert ran == []
    # Every answer is a valid ElicitResult.
    for _, answer in buttons(outcome):
        ElicitResult.model_validate(answer)


async def test_one_boolean_gives_yes_no_cancel() -> None:
    outcome, _, _ = await ask("replace")
    assert buttons(outcome) == [
        ("Yes", {"action": "accept", "content": {"sure": True}}),
        ("No", {"action": "accept", "content": {"sure": False}}),
        ("Cancel", {"action": "decline"}),
    ]


async def test_choices_give_one_button_each_and_cancel() -> None:
    outcome, _, _ = await ask("pick_quality")
    assert buttons(outcome) == [
        ("1080p", {"action": "accept", "content": {"quality": "1080p"}}),
        ("4K", {"action": "accept", "content": {"quality": "4K"}}),
        ("Cancel", {"action": "decline"}),
    ]


def _elicit(schema: dict) -> InputRequiredResult:
    return InputRequiredResult(
        input_requests={
            "k": ElicitRequest(
                params=ElicitRequestFormParams(message="Pick", requested_schema=schema)
            )
        },
        request_state="s",
    )


def test_titled_choices_use_their_titles() -> None:
    schema = {
        "type": "object",
        "properties": {
            "quality": {
                "type": "string",
                "oneOf": [{"const": "hd", "title": "1080p"}, {"const": "uhd", "title": "4K"}],
            }
        },
    }
    outcome = _classify("mcparr", "mcparr_pick", _elicit(schema), 1)
    assert buttons(outcome) == [
        ("1080p", {"action": "accept", "content": {"quality": "hd"}}),
        ("4K", {"action": "accept", "content": {"quality": "uhd"}}),
        ("Cancel", {"action": "decline"}),
    ]


@pytest.mark.parametrize(
    ("tool", "what"),
    [
        ("name_it", "a string field"),
        ("two_fields", "2 fields"),
        ("nine_choices", "9 choices"),
        ("batched", "2 questions at once"),
    ],
)
async def test_questions_alfred_cant_show(tool: str, what: str) -> None:
    outcome, opened, ran = await ask(tool)
    assert outcome == ToolOutcome(
        False,
        f"Error: mcparr asked for input alfred can't show ({what}); the call did not complete.",
    )
    # Nothing was sent back: one connection, and the tool never ran.
    assert opened == ["mcparr"] and ran == []


async def test_sampling_request_is_an_error_result() -> None:
    # The server refuses it first, as alfred declares no sampling.
    outcome, opened, ran = await ask("summarise")
    assert not outcome.ok and outcome.need is None and "sampling" in outcome.text
    assert opened == ["mcparr"] and ran == []


def test_url_and_sampling_requests_cant_be_shown() -> None:
    url = InputRequiredResult(
        input_requests={
            "k": ElicitRequest(
                params=ElicitRequestURLParams(
                    message="Log in", url="https://example.com", elicitation_id="e"
                )
            )
        },
        request_state="s",
    )
    sampling = InputRequiredResult(
        input_requests={
            "k": CreateMessageRequest(
                params=CreateMessageRequestParams(
                    messages=[
                        SamplingMessage(role="user", content=TextContent(type="text", text="x"))
                    ],
                    max_tokens=5,
                )
            )
        },
    )
    assert "(url mode)" in _classify("mcparr", "t", url, 1).text
    assert "(a sampling/createMessage request)" in _classify("mcparr", "t", sampling, 1).text


async def test_ordinary_calls_are_unchanged() -> None:
    connect = in_process({"mcparr": make_mcparr()})
    turn = await build_turn_tools([MCPARR], connect, 5)
    outcome = await call_tool(
        turn, "mcparr_add_media", '{"title": "Dune"}', connect=connect, call_timeout_s=5,
        max_chars=999,
    )  # fmt: skip
    assert outcome == ToolOutcome(True, json.dumps({"result": "added Dune"}))
