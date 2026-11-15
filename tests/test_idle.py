"""Closing idle conversations (change close-idle-conversations, design D3)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.models import Conversation, Entry, QueueItem
from alfred.history import History
from alfred.idle import IdleCloser
from tests.helpers import add_queue_item

SIX_HOURS = timedelta(hours=6)


class Clock:
    """Starts at the real time, so it agrees with the entries' database times."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


async def wrote(
    sessions: async_sessionmaker[AsyncSession], user: str, at: datetime, kind: str = "user"
) -> int:
    """An entry for ``user`` recorded at ``at``. Returns its conversation."""
    history = History(sessions)
    source = "backup" if kind == "notice" else None
    entry_id = await history.append(user, kind, "hi", source=source)  # type: ignore[arg-type]
    async with sessions.begin() as session:
        await session.execute(update(Entry).where(Entry.id == entry_id).values(created_at=at))
    return await history.current_conversation_id(user)


def closer(
    sessions: async_sessionmaker[AsyncSession], clock: Clock, nudges: list[int] | None = None
) -> IdleCloser:
    return IdleCloser(
        History(sessions),
        SIX_HOURS,
        nudge=(lambda: nudges.append(1)) if nudges is not None else None,
        now=clock,
    )


async def ended(sessions: async_sessionmaker[AsyncSession], cid: int) -> str | None:
    async with sessions() as session:
        row = await session.get(Conversation, cid)
    assert row is not None
    return row.end_reason


async def test_closes_at_the_due_time_and_not_before(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock()
    nudges: list[int] = []
    idle = closer(sessions, clock, nudges)
    cid = await wrote(sessions, "diogo", clock.now - timedelta(hours=5))
    # Due in an hour: sleep exactly that long, close nothing.
    assert await idle.close_once() == pytest.approx(3600)
    assert await ended(sessions, cid) is None

    clock.now += timedelta(hours=1, seconds=1)
    assert await idle.close_once() == SIX_HOURS.total_seconds()  # nothing current is due
    assert await ended(sessions, cid) == "idle"
    assert nudges == [1]
    assert await History(sessions).current_conversation_id("diogo") != cid


async def test_a_notice_does_not_delay_it(sessions: async_sessionmaker[AsyncSession]) -> None:
    clock = Clock()
    idle = closer(sessions, clock)
    # Last wrote at 23:10, a notice at 02:00, now 05:11.
    cid = await wrote(sessions, "diogo", clock.now - timedelta(hours=6, minutes=1))
    await wrote(sessions, "diogo", clock.now - timedelta(hours=3, minutes=11), kind="notice")
    await idle.close_once()
    assert await ended(sessions, cid) == "idle"


async def test_only_notices_never_close(sessions: async_sessionmaker[AsyncSession]) -> None:
    clock = Clock()
    idle = closer(sessions, clock)
    cid = await wrote(sessions, "diogo", clock.now - timedelta(days=4), kind="notice")
    assert await idle.close_once() == SIX_HOURS.total_seconds()
    assert await ended(sessions, cid) is None


async def test_a_skipped_candidate_is_retried(sessions: async_sessionmaker[AsyncSession]) -> None:
    clock = Clock()
    idle = closer(sessions, clock)
    ana = await wrote(sessions, "ana", clock.now - timedelta(hours=7))
    diogo = await wrote(sessions, "diogo", clock.now - timedelta(hours=7))
    item = await add_queue_item(sessions, user="ana")  # her message waits in the queue
    assert await idle.close_once() == 60  # look again soon
    assert await ended(sessions, ana) is None
    assert await ended(sessions, diogo) == "idle"  # other users aren't held up

    async with sessions.begin() as session:
        await session.execute(update(QueueItem).where(QueueItem.id == item).values(status="done"))
    clock.now += timedelta(seconds=60)
    await idle.close_once()
    assert await ended(sessions, ana) == "idle"


async def test_sleep_never_passes_a_new_due_time(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock()
    idle = closer(sessions, clock)
    # Nothing current: sleep the whole idle time...
    wait = await idle.close_once()
    assert wait == SIX_HOURS.total_seconds()
    # ...and a first message during the sleep is due no sooner than its end.
    clock.now += timedelta(seconds=1)
    await wrote(sessions, "diogo", clock.now)
    clock.now += timedelta(seconds=wait - 1)
    assert await idle.close_once() == pytest.approx(1)


async def test_run_forever_sleeps_as_told(sessions: async_sessionmaker[AsyncSession]) -> None:
    clock = Clock()
    sleeps: list[float] = []

    class Stop(Exception):
        pass

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise Stop
        clock.now += timedelta(seconds=seconds)

    cid = await wrote(sessions, "diogo", clock.now - timedelta(hours=2))
    idle = IdleCloser(History(sessions), SIX_HOURS, now=clock, sleep=sleep)
    with pytest.raises(Stop):
        await asyncio.wait_for(idle.run_forever(), 5)
    assert sleeps == [pytest.approx(4 * 3600), SIX_HOURS.total_seconds()]
    assert await ended(sessions, cid) == "idle"
