"""The attachment tools in whole turns, with the toolsets ``serve`` builds."""

from __future__ import annotations

import json

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.brains import Brains
from alfred.attachments.tools import READ_DOCUMENT, TEXT_ONLY_NOTE, VIEW_ATTACHMENT, VISION_NOTE
from alfred.config import BrainEntry
from alfred.history import History
from alfred.memory.search import RECALL_CONVERSATION, SEARCH_CONVERSATIONS
from alfred.tools.base import TurnInput
from alfred.tools.brains import SET_MODEL
from alfred.tools.calculator import CALCULATE
from alfred.tools.conversation import END_CONVERSATION
from alfred.tools.dates import DATE
from alfred.web.tools import WEB_FETCH, WEB_SEARCH
from tests.agent.test_turn import MCP_TOOLS
from tests.agent.turn_helpers import (
    GEMMA,
    NOW,
    Clock,
    agent,
    memory,
    reminder_tools,
    tool_rows,
    web_tools,
)
from tests.attachments.files import image_bytes
from tests.helpers import (
    FakeEmbedder,
    FakeModel,
    FakeStore,
    calls,
    image_file,
    owner_with_files,
    text,
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
    await a.run(TurnInput("owner", "hi", item))
    assert [t["function"]["name"] for t in model.requests[0][1]] == [
        WEB_SEARCH,
        WEB_FETCH,
        END_CONVERSATION,
        SEARCH_CONVERSATIONS,
        RECALL_CONVERSATION,
        VIEW_ATTACHMENT,
        READ_DOCUMENT,
        "alfred_remind_set",
        "alfred_reminders_list",
        "alfred_reminder_cancel",
        SET_MODEL,
        CALCULATE,
        DATE,
        *MCP_TOOLS,
    ]


async def test_no_attachment_tools_without_the_section(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    item, _ = await owner_with_files(sessions, "hi", [])
    await agent(sessions, model, brains=vision_brains(), store=FakeStore()).run(
        TurnInput("owner", "hi", item)
    )
    names = {t["function"]["name"] for t in model.requests[0][1]}
    assert not names & {VIEW_ATTACHMENT, READ_DOCUMENT}
    assert "[attachment #" not in model.requests[0][0][0]["content"]


async def test_owner_turn_views_an_earlier_photo(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = FakeStore()
    await owner_with_files(
        sessions, "what's the total?", [image_file(name="receipt.jpg")], store, [image_bytes()]
    )
    await History(sessions).append("assistant", "12.40 EUR, sir.")
    item, _ = await owner_with_files(sessions, "and the date on it?", [])
    model = FakeModel([calls((VIEW_ATTACHMENT, json.dumps({"id": 1}))), text("9 November, sir.")])
    a = agent(sessions, model, brains=vision_brains(), store=store, attachments=ATTACHMENTS)
    result = await a.run(TurnInput("owner", "and the date on it?", item))

    assert result.reply == "9 November, sir."
    system = model.requests[0][0][0]["content"]
    assert VISION_NOTE in system
    tool_result = model.requests[1][0][-1]
    assert tool_result["role"] == "tool"
    assert tool_result["content"][0]["text"].startswith('[attachment #1: image "receipt.jpg"]')
    assert tool_result["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    (row,) = await tool_rows(sessions)
    assert (row.tool, row.ok) == (VIEW_ATTACHMENT, True)
    assert row.result_excerpt.endswith("[1 image]")


async def test_text_only_brain_explains_no_view_tool(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel([text("ok")])
    item, _ = await owner_with_files(sessions, "hi", [])
    a = agent(
        sessions, model, brains=vision_brains(False), store=FakeStore(), attachments=ATTACHMENTS
    )
    await a.run(TurnInput("owner", "hi", item))
    system = model.requests[0][0][0]["content"]
    assert TEXT_ONLY_NOTE in system and VIEW_ATTACHMENT not in system
    names = {t["function"]["name"] for t in model.requests[0][1]}
    assert READ_DOCUMENT in names and VIEW_ATTACHMENT not in names
