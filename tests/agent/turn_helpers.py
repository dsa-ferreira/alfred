"""Building agents and turns for the tests of a whole turn."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.brains import Brains
from alfred.agent.model import ModelFailed, ModelReply
from alfred.agent.prompts import OPERATING_RULES
from alfred.agent.turn import Agent
from alfred.db.models import TurnToolCall
from alfred.history import History
from alfred.mcp.registry import ServerInstructions
from alfred.mcp.toolset import server_instructions
from alfred.memory.search import ConversationSearch
from alfred.reminders.store import Reminders
from alfred.reminders.tools import ReminderTools
from alfred.tools.base import TurnInput
from alfred.tools.brains import SET_MODEL
from alfred.web.fetch import Fetcher
from alfred.web.search import SearxngClient
from alfred.web.tools import WEB_FETCH, WEB_SEARCH, WebTools
from tests.helpers import FakeEmbedder, FakeModel, add_queue_item, calls, make_agent, make_config
from tests.mcp.servers import COOKBOOK_INSTRUCTIONS, in_process, make_cookbook, make_mcparr
from tests.mcp.servers import config as server_config
from tests.web.test_fetch import Resolver
from tests.web.test_search import SEARXNG

MCPARR = server_config("mcparr")


COOKBOOK = server_config("cookbook")


COOKBOOK_SECTION = server_instructions(ServerInstructions("cookbook", COOKBOOK_INSTRUCTIONS))


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
    memory: ConversationSearch | None = None,
    web: WebTools | None = None,
    brains: Brains | None = None,
    reminders: ReminderTools | None = None,
    now: datetime | Callable[[], datetime] = NOW,
    **config: object,
) -> Agent:
    cfg = make_config(mcp_servers=[MCPARR, COOKBOOK], **config)  # type: ignore[arg-type]
    connect = in_process({"mcparr": make_mcparr(), "cookbook": make_cookbook()})
    return make_agent(
        cfg,
        model,
        History(sessions),
        sessions,
        connect,
        memory=memory,
        web=web,
        reminders=reminders,
        brains=brains,
        now=now if callable(now) else lambda: now,
    )


def memory(
    sessions: async_sessionmaker[AsyncSession], embedder: FakeEmbedder
) -> ConversationSearch:
    return ConversationSearch(make_config(), sessions, embedder)


async def owner_turn(sessions: async_sessionmaker[AsyncSession], message: str) -> TurnInput:
    item = await add_queue_item(sessions, "owner", message)
    await History(sessions).append("owner", message, queue_item_id=item)
    return TurnInput("owner", message, item)


async def tool_rows(sessions: async_sessionmaker[AsyncSession]) -> list[TurnToolCall]:
    async with sessions() as session:
        return list(await session.scalars(select(TurnToolCall).order_by(TurnToolCall.id)))


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


LISBON = ZoneInfo("Europe/Lisbon")


DUE = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)  # 17:00 in Lisbon (WET)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def reminder_tools(sessions: async_sessionmaker[AsyncSession], clock: Clock) -> ReminderTools:
    return ReminderTools(Reminders(sessions), LISBON, "09:00", now=clock)


async def reminder_turn(
    sessions: async_sessionmaker[AsyncSession], reminder_id: int = 12, text_: str = "call mum"
) -> TurnInput:
    item = await add_queue_item(sessions, "reminder", text_)
    return TurnInput("reminder", text_, item, reminder_id=reminder_id, due_at=DUE)
