"""Web search and fetch in a turn (change add-web-search)."""

from __future__ import annotations

import httpx
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.prompts import NO_REPLY
from alfred.db.models import Conversation, Entry
from alfred.history import History
from alfred.tools.base import TurnInput
from alfred.tools.conversation import END_CONVERSATION
from alfred.web.tools import WEB_FETCH, WEB_SEARCH
from tests.agent.turn_helpers import (
    NOW,
    agent,
    fetch_call,
    last_tool_result,
    owner_turn,
    search_call,
    tool_rows,
    web_tools,
)
from tests.helpers import FakeModel, add_queue_item, make_config, text
from tests.web.test_search import SEARXNG
from tests.web.test_search import reply as search_reply

# --- web tools (change add-web-search) ----------------------------------------------


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
