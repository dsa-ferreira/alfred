from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.app import _TOOLSET_TYPES, _build_toolsets, serve
from alfred.archive.search import ConversationSearch
from alfred.attachments.tools import AttachmentTools
from alfred.brains.registry import Brains
from alfred.conversations.store import History
from alfred.db.models import Conversation, QueueItem, Reminder, ReminderRecipient
from alfred.facts.store import FactStore
from alfred.facts.tools import FactTools
from alfred.kernel.access import AllowList
from alfred.kernel.turn import AskTurn, ChatTurn, ReminderTurn
from alfred.kernel.users import Users
from alfred.mcp.toolset import McpTools
from alfred.reminders.tools import ReminderTools
from alfred.settings import ConfigError
from alfred.startup import check_tool_names, warn_unknown_users
from alfred.web.tools import WebTools
from tests.helpers import ANA, DIOGO, FakeEmbedder, FakeStore, make_config


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions.begin() as session:
        session.add(Conversation(user_name="diogo"))
        session.add(QueueItem(kind="user", user_name="ana", text="hi"))
        reminder = Reminder(text="x", due_at=datetime.now(UTC), created_by="ana")
        session.add(reminder)
        await session.flush()
        session.add(ReminderRecipient(reminder_id=reminder.id, user_name="dioog"))


async def test_unknown_user_is_named(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    await _rows(sessions)
    with caplog.at_level(logging.WARNING, logger="alfred.startup"):
        unknown = await warn_unknown_users(sessions, ["diogo", "ana"])
    assert unknown == ["dioog"]
    (record,) = caplog.records
    assert "'dioog'" in record.getMessage() and "diogo, ana" in record.getMessage()


async def test_no_warning_when_all_are_configured(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    await _rows(sessions)
    with caplog.at_level(logging.WARNING, logger="alfred.startup"):
        unknown = await warn_unknown_users(sessions, ["diogo", "ana", "dioog"])
    assert unknown == [] and caplog.records == []


async def test_empty_database(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="alfred.startup"):
        assert await warn_unknown_users(sessions, ["diogo"]) == []
    assert caplog.records == []


# --- Tool names in allow-lists ----------------------------------------------------


async def test_every_offered_name_is_declared() -> None:
    """Each toolset offers only names its class declares, on any turn and brain."""
    config = make_config(
        ai={"models": {"seeing": {"model": "v", "vision": True}}},
        memory={"facts_in_context": True},
        attachments={"endpoint_url": "http://s3.lan", "bucket": "b"},
    )
    brains = Brains.from_config(config.ai)
    sessions: Any = None  # nothing here touches the database
    assert config.attachments is not None
    toolsets = _build_toolsets(
        config,
        lambda server: None,  # type: ignore[arg-type,return-value]
        brains=brains,
        web=WebTools.from_config(config.web, max_chars=4000),
        memory=ConversationSearch(
            config.memory, ZoneInfo(config.ai.timezone), sessions, FakeEmbedder()
        ),
        facts=FactTools(
            FactStore(config.memory, config.queue, sessions, FakeEmbedder()), config.memory
        ),
        reminders=ReminderTools.from_config(
            config.reminders, ZoneInfo(config.ai.timezone), config.users, sessions
        ),
        attachments=AttachmentTools(
            FakeStore(), sessions, brains, config.attachments, History(sessions)
        ),
    )
    local = [t for t in toolsets if not isinstance(t, McpTools)]
    assert [type(t) for t in local] == list(_TOOLSET_TYPES)
    turns = [
        ChatTurn("hi", 1, user=DIOGO),
        AskTurn("hi", 2, "disk-check", user=DIOGO),
        ReminderTurn("hi", 3, user=DIOGO),
    ]
    for brain in ("seeing", brains.current.name):
        brains.set(brain)
        for toolset in local:
            for turn in turns:
                opened = await toolset.open(turn)
                offered = {d["function"]["name"] for d in opened.definitions} if opened else set()
                assert offered <= type(toolset).names, type(toolset).__name__


@pytest.mark.parametrize("key", ["tools", "confirm"])
def test_unknown_alfred_name_is_a_config_error(key: str) -> None:
    ana = dataclasses.replace(ANA, **{key: AllowList.of("alfred_*", "alfred_nope")})
    with pytest.raises(ConfigError) as exc:
        check_tool_names(Users((DIOGO, ana)), _TOOLSET_TYPES)
    assert exc.value.problems == [
        f"users[1] (ana): {key} entry 'alfred_nope' is not an alfred tool"
    ]


def test_known_names_and_patterns_pass() -> None:
    ana = dataclasses.replace(
        ANA, tools=AllowList.of("alfred_remind_set", "alfred_nothing_*"), confirm=AllowList.of("*")
    )
    check_tool_names(Users((DIOGO, ana)), _TOOLSET_TYPES)


async def test_checked_before_connecting() -> None:
    """A typo fails as a config error even when the database can't be reached."""
    ana = dataclasses.replace(ANA, tools=AllowList.of("alfred_nope"))
    config = dataclasses.replace(
        make_config(users=(DIOGO, ana)),
        database_url=SecretStr("postgresql+psycopg://alfred@127.0.0.1:1/nowhere"),
    )
    with pytest.raises(ConfigError, match="is not an alfred tool"):
        await serve(config)
