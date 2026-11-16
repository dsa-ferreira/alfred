"""The attachment tools in whole turns, with the toolsets ``serve`` builds."""

from __future__ import annotations

import json

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.features.archive.tools import _RECALL_CONVERSATION, _SEARCH_CONVERSATIONS
from alfred.features.attachments.tools import (
    _READ_DOCUMENT,
    _TEXT_ONLY_NOTE,
    _VIEW_ATTACHMENT,
    _VISION_NOTE,
)
from alfred.features.brains.config import BrainEntry
from alfred.features.brains.registry import Brains
from alfred.features.brains.tools import _SET_MODEL
from alfred.features.conversations.store import History
from alfred.features.conversations.tools import _END_CONVERSATION
from alfred.features.toolbox.calculator import _CALCULATE
from alfred.features.toolbox.dates import _DATE
from alfred.features.web.tools import _WEB_FETCH, _WEB_SEARCH
from alfred.kernel.turn import ChatTurn
from tests.features.attachments.files import image_bytes
from tests.helpers import (
    DIOGO,
    FakeEmbedder,
    FakeModel,
    FakeStore,
    calls,
    image_file,
    owner_with_files,
    text,
)
from tests.runtime.agent.test_turn import MCP_TOOLS
from tests.runtime.agent.turn_helpers import (
    GEMMA,
    NOW,
    Clock,
    agent,
    memory,
    reminder_tools,
    tool_rows,
    web_tools,
)

ATTACHMENTS = {"endpoint_url": "http://garage.lan:3900", "bucket": "alfred"}


def vision_brains(vision: bool = True) -> Brains:
    return Brains(
        GEMMA,
        {"gemma": BrainEntry(model=GEMMA, vision=vision), "bonsai": "bonsai:8b"},
    )


async def test_tool_order_with_attachments(
    sessions: async_sessionmaker[AsyncSession], web_http: httpx.AsyncClient
) -> None:
    model = FakeModel([text("ok")])
    item, _ = await owner_with_files(sessions, "hi", [])
    a = agent(
        sessions,
        model,
        memory=memory(sessions, FakeEmbedder()),
        web=web_tools(web_http),
        reminders=reminder_tools(sessions, Clock(NOW)),
        brains=vision_brains(),
        store=FakeStore(),
        attachments=ATTACHMENTS,
    )
    turn = ChatTurn("hi", item, user=DIOGO)
    await a.run(turn)
    assert [t["function"]["name"] for t in model.requests[0][1]] == [
        _WEB_SEARCH,
        _WEB_FETCH,
        _END_CONVERSATION,
        _SEARCH_CONVERSATIONS,
        _RECALL_CONVERSATION,
        _VIEW_ATTACHMENT,
        _READ_DOCUMENT,
        "alfred_remind_set",
        "alfred_reminders_list",
        "alfred_reminder_cancel",
        _SET_MODEL,
        _CALCULATE,
        _DATE,
        *MCP_TOOLS,
    ]


async def test_no_attachment_tools_without_the_section(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    item, _ = await owner_with_files(sessions, "hi", [])
    await agent(sessions, model, brains=vision_brains(), store=FakeStore()).run(
        ChatTurn("hi", item, user=DIOGO)
    )
    names = {t["function"]["name"] for t in model.requests[0][1]}
    assert not names & {_VIEW_ATTACHMENT, _READ_DOCUMENT}
    assert "[attachment #" not in model.requests[0][0][0]["content"]


async def test_owner_turn_views_an_earlier_photo(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = FakeStore()
    await owner_with_files(
        sessions, "what's the total?", [image_file(name="receipt.jpg")], store, [image_bytes()]
    )
    await History(sessions).append("diogo", "assistant", "12.40 EUR, sir.")
    item, _ = await owner_with_files(sessions, "and the date on it?", [])
    model = FakeModel([calls((_VIEW_ATTACHMENT, json.dumps({"id": 1}))), text("9 November, sir.")])
    a = agent(sessions, model, brains=vision_brains(), store=store, attachments=ATTACHMENTS)
    turn = ChatTurn("and the date on it?", item, user=DIOGO)
    result = await a.run(turn)

    assert result.reply == "9 November, sir."
    system = model.requests[0][0][0]["content"]
    assert _VISION_NOTE in system
    tool_result = model.requests[1][0][-1]
    assert tool_result["role"] == "tool"
    assert tool_result["content"][0]["text"].startswith('[attachment #1: image "receipt.jpg"]')
    assert tool_result["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    (row,) = await tool_rows(sessions)
    assert (row.tool, row.ok) == (_VIEW_ATTACHMENT, True)
    assert row.result_excerpt.endswith("[1 image]")


async def test_text_only_brain_explains_no_view_tool(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    item, _ = await owner_with_files(sessions, "hi", [])
    a = agent(
        sessions, model, brains=vision_brains(False), store=FakeStore(), attachments=ATTACHMENTS
    )
    turn = ChatTurn("hi", item, user=DIOGO)
    await a.run(turn)
    system = model.requests[0][0][0]["content"]
    assert _TEXT_ONLY_NOTE in system and _VIEW_ATTACHMENT not in system
    names = {t["function"]["name"] for t in model.requests[0][1]}
    assert _READ_DOCUMENT in names and _VIEW_ATTACHMENT not in names
