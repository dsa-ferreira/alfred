from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.models import Conversation, QueueItem, Reminder, ReminderRecipient
from alfred.db.users import warn_unknown_users


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
    with caplog.at_level(logging.WARNING, logger="alfred.db.users"):
        unknown = await warn_unknown_users(sessions, ["diogo", "ana"])
    assert unknown == ["dioog"]
    (record,) = caplog.records
    assert "'dioog'" in record.getMessage() and "diogo, ana" in record.getMessage()


async def test_no_warning_when_all_are_configured(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    await _rows(sessions)
    with caplog.at_level(logging.WARNING, logger="alfred.db.users"):
        unknown = await warn_unknown_users(sessions, ["diogo", "ana", "dioog"])
    assert unknown == [] and caplog.records == []


async def test_empty_database(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="alfred.db.users"):
        assert await warn_unknown_users(sessions, ["diogo"]) == []
    assert caplog.records == []
