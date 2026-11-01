from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from mcp import Client

from alfred.config import McpServer
from alfred.mcp.registry import build_turn_tools, call_tool, tool_name
from tests.mcp.servers import config, in_process, make_cookbook, make_mcparr

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
    assert tool_name("my-server", "get.things now") == "my-server_get_things_now"


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
