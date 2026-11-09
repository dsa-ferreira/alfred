"""The MCP servers as one toolset: routing, exclusions, groups and prompt notes."""

from __future__ import annotations

import json

from alfred.agent.toolset import AttemptContext, PromptNote, dispatch
from alfred.agent.turn import TurnInput
from alfred.mcp.registry import ServerInstructions
from alfred.mcp.toolset import McpTools, server_instructions, unavailable_note
from tests.mcp.servers import COOKBOOK_INSTRUCTIONS, config, in_process, make_cookbook, make_mcparr

MCPARR = config("mcparr")
COOKBOOK = config("cookbook", frozenset({"delete_recipe"}))
TURN = TurnInput("owner", "hi", 1)


def mcp_tools(**servers: object) -> McpTools:
    connect = in_process(servers)  # type: ignore[arg-type]
    return McpTools(
        [MCPARR, COOKBOOK],
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
