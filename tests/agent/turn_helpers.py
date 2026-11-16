"""Building agents and turns for the tests of a whole turn."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.messages import strip_stamp
from alfred.agent.prompts import _CALCULATE, _DATE, _operating_rules
from alfred.agent.results import TurnResult
from alfred.agent.turn import Agent
from alfred.archive.search import ConversationSearch
from alfred.archive.summaries import recalled
from alfred.brains.registry import Brains
from alfred.brains.tools import _SET_MODEL
from alfred.config import _MemoryConfig
from alfred.conversations.store import History
from alfred.facts.tools import FactTools
from alfred.kernel.turn import ChatTurn, ReminderTurn, Resume, TurnInput
from alfred.kernel.users import User, Users
from alfred.llm.chat import ModelFailed, ModelReply
from alfred.mcp.registry import ServerInstructions
from alfred.mcp.toolset import _server_instructions
from alfred.reminders.store import Reminders
from alfred.reminders.tools import ReminderTools
from alfred.turns.tables import TurnToolCall
from alfred.web.fetch import Fetcher
from alfred.web.search import SearxngClient
from alfred.web.tools import _WEB_FETCH, _WEB_SEARCH, WebTools
from tests.helpers import (
    DIOGO,
    FakeEmbedder,
    FakeModel,
    FakeStore,
    add_queue_item,
    calls,
    make_agent,
    make_config,
)
from tests.mcp.servers import COOKBOOK_INSTRUCTIONS, in_process, make_cookbook, make_mcparr
from tests.mcp.servers import config as server_config
from tests.web.test_fetch import Resolver
from tests.web.test_search import SEARXNG

MCPARR = server_config("mcparr")


COOKBOOK = server_config("cookbook")


COOKBOOK_SECTION = _server_instructions(ServerInstructions("cookbook", COOKBOOK_INSTRUCTIONS))


NOW = datetime(2026, 11, 2, 14, 5, tzinfo=UTC)


def rules(*tools: str, user: str = "diogo", others: tuple[str, ...] = ()) -> str:
    """The operating rules, date and brain line of a turn of the default test agent.

    It offers the calculator and dates on every turn; ``tools`` are the other
    alfred tools offered.
    """
    return (
        _operating_rules(user, {_CALCULATE, _DATE, *tools}, others)
        + "\n\nToday is Monday, 2 November 2026."
        + "\n\nYou are running on the model gemma4:26b-a4b-it-qat."
    )


RULES = rules()


def agent(
    sessions: async_sessionmaker[AsyncSession],
    model: FakeModel,
    *,
    memory: ConversationSearch | None = None,
    web: WebTools | None = None,
    brains: Brains | None = None,
    reminders: ReminderTools | None = None,
    store: FakeStore | None = None,
    facts: FactTools | None = None,
    memory_config: dict[str, Any] | None = None,
    now: datetime | Callable[[], datetime] = NOW,
    **config: object,
) -> Agent:
    """The default test agent; ``memory_config`` is its ``[memory]`` section."""
    cfg = make_config(
        mcp_servers=[MCPARR, COOKBOOK],
        memory=memory_config,
        **config,  # type: ignore[arg-type]
    )
    connect = in_process({"mcparr": make_mcparr(), "cookbook": make_cookbook()})
    return make_agent(
        cfg,
        model,
        History(sessions, recalled=recalled),
        sessions,
        connect,
        memory=memory,
        web=web,
        reminders=reminders,
        brains=brains,
        store=store,
        facts=facts,
        now=now if callable(now) else lambda: now,
    )


def memory(
    sessions: async_sessionmaker[AsyncSession], embedder: FakeEmbedder
) -> ConversationSearch:
    return ConversationSearch(_MemoryConfig(), LISBON, sessions, embedder)


async def owner_turn(
    sessions: async_sessionmaker[AsyncSession], message: str, user: User = DIOGO
) -> TurnInput:
    """``user``'s chat message, queued and recorded as the worker does."""
    item = await add_queue_item(sessions, "chat", message, user=user.name)
    await History(sessions).append(user.name, "user", message, turn_id=item)
    return ChatTurn(message, item, user=user)


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
    return calls((_SET_MODEL, json.dumps({"model": name})))


def contents(messages: list[dict[str, Any]]) -> list[str]:
    """The messages' text without their times (stored entries carry the database's clock)."""
    return [strip_stamp(m["content"]) for m in messages]


def tool_result(model: FakeModel, request: int) -> str:
    return model.requests[request][0][-1]["content"]


def web_tools(http: httpx.AsyncClient, *, page_chars: int = 8000) -> WebTools:
    search = SearxngClient(http, SEARXNG, results=5, timeout_s=5, max_chars=4000)
    fetcher = Fetcher(http, timeout_s=5, max_bytes=5_000_000, resolver=Resolver())
    return WebTools(search, fetcher, page_chars=page_chars, close=http.aclose)


def fetch_call(url: str, **extra: object) -> ModelReply:
    return calls((_WEB_FETCH, json.dumps({"url": url, **extra})))


def search_call(query: str) -> ModelReply:
    return calls((_WEB_SEARCH, json.dumps({"query": query})))


def last_tool_result(model: FakeModel) -> str:
    return model.requests[-1][0][-1]["content"]


LISBON = ZoneInfo("Europe/Lisbon")


DUE = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)  # 17:00 in Lisbon (WET)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def reminder_tools(
    sessions: async_sessionmaker[AsyncSession], clock: Clock, users: Sequence[User] = (DIOGO,)
) -> ReminderTools:
    return ReminderTools(Reminders(sessions), LISBON, "09:00", Users(tuple(users)), now=clock)


async def reminder_turn(
    sessions: async_sessionmaker[AsyncSession],
    reminder_id: int = 12,
    text_: str = "call mum",
    user: User = DIOGO,
    set_by: str | None = None,
    *,
    due_at: datetime = DUE,
    repeat_label: str | None = None,
    quiet_ok: bool = False,
) -> TurnInput:
    item = await add_queue_item(sessions, "reminder", text_, user=user.name)
    return ReminderTurn(
        text_,
        item,
        reminder_id=reminder_id,
        due_at=due_at,
        set_by=set_by,
        repeat_label=repeat_label,
        quiet_ok=quiet_ok,
        user=user,
    )


def resumed(turn: TurnInput, result: TurnResult, answer: object) -> TurnInput:
    """``turn`` going on after its user chose ``answer``, as stored in the database."""
    waiting = result.waiting
    assert waiting is not None, result
    saved = json.loads(json.dumps(waiting.checkpoint.to_json()))
    state = json.loads(json.dumps(waiting.need.state))
    return dataclasses.replace(turn, resume=Resume(saved, state, answer))
