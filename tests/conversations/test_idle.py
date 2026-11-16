"""Closing idle conversations (change close-idle-conversations, design D3)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.conversations.idle import IdleCloser
from alfred.conversations.store import History
from alfred.conversations.tables import Conversation, Entry
from alfred.turns.tables import QueueItem
from tests.fakes import QuietQueue, ignore
from tests.helpers import add_queue_item, make_sweeper

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
        nudge=(lambda: nudges.append(1)) if nudges is not None else ignore,
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
    assert await idle._close_once() == pytest.approx(3600)
    assert await ended(sessions, cid) is None

    clock.now += timedelta(hours=1, seconds=1)
    assert await idle._close_once() == SIX_HOURS.total_seconds()  # nothing current is due
    assert await ended(sessions, cid) == "idle"
    assert nudges == [1]
    assert await History(sessions).current_conversation_id("diogo") != cid


async def test_a_notice_does_not_delay_it(sessions: async_sessionmaker[AsyncSession]) -> None:
    clock = Clock()
    idle = closer(sessions, clock)
    # Last wrote at 23:10, a notice at 02:00, now 05:11.
    cid = await wrote(sessions, "diogo", clock.now - timedelta(hours=6, minutes=1))
    await wrote(sessions, "diogo", clock.now - timedelta(hours=3, minutes=11), kind="notice")
    await idle._close_once()
    assert await ended(sessions, cid) == "idle"


async def test_only_notices_never_close(sessions: async_sessionmaker[AsyncSession]) -> None:
    clock = Clock()
    idle = closer(sessions, clock)
    cid = await wrote(sessions, "diogo", clock.now - timedelta(days=4), kind="notice")
    assert await idle._close_once() == SIX_HOURS.total_seconds()
    assert await ended(sessions, cid) is None


async def test_a_skipped_candidate_is_retried(sessions: async_sessionmaker[AsyncSession]) -> None:
    clock = Clock()
    idle = closer(sessions, clock)
    ana = await wrote(sessions, "ana", clock.now - timedelta(hours=7))
    diogo = await wrote(sessions, "diogo", clock.now - timedelta(hours=7))
    item = await add_queue_item(sessions, user="ana")  # her message waits in the queue
    assert await idle._close_once() == 60  # look again soon
    assert await ended(sessions, ana) is None
    assert await ended(sessions, diogo) == "idle"  # other users aren't held up

    async with sessions.begin() as session:
        await session.execute(update(QueueItem).where(QueueItem.id == item).values(status="done"))
    clock.now += timedelta(seconds=60)
    await idle._close_once()
    assert await ended(sessions, ana) == "idle"


async def test_sleep_never_passes_a_new_due_time(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock()
    idle = closer(sessions, clock)
    # Nothing current: sleep the whole idle time...
    wait = await idle._close_once()
    assert wait == SIX_HOURS.total_seconds()
    # ...and a first message during the sleep is due no sooner than its end.
    clock.now += timedelta(seconds=1)
    await wrote(sessions, "diogo", clock.now)
    clock.now += timedelta(seconds=wait - 1)
    assert await idle._close_once() == pytest.approx(1)


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
    idle = IdleCloser(History(sessions), SIX_HOURS, nudge=ignore, now=clock, sleep=sleep)
    with pytest.raises(Stop):
        await asyncio.wait_for(idle.run_forever(), 5)
    assert sleeps == [pytest.approx(4 * 3600), SIX_HOURS.total_seconds()]
    assert await ended(sessions, cid) == "idle"


async def test_memory_command_is_not_a_user_message(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """/memory leaves the last user message as it was (change add-user-facts, D11)."""
    from tests.telegram.test_bot import memory_dispatcher, memory_texts

    clock = Clock()
    earlier = clock.now - timedelta(hours=5)
    cid = await wrote(sessions, "diogo", earlier)
    dispatcher, _ = memory_dispatcher(sessions)
    await memory_texts(sessions, dispatcher, "/memory")
    [last] = await History(sessions).last_user_messages()
    assert (last.conversation_id, last.at) == (cid, earlier)
    # Still due an hour after the earlier message's five, not six hours from now.
    assert await closer(sessions, clock)._close_once() == pytest.approx(3600)
    clock.now += timedelta(hours=1, seconds=1)
    await closer(sessions, clock)._close_once()
    assert await ended(sessions, cid) == "idle"


# --- unattended conversations (change add-recurring-reminders, design D7) -------------


async def test_unattended_conversation_is_summarised_without_fact_requests(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from alfred.archive.summaries import Summaries
    from alfred.facts.extraction_job import FactExtractionJob
    from alfred.facts.store import FactStore
    from tests.helpers import FakeEmbedder, FakeModel, make_config, text
    from tests.turns.test_background import until

    cfg = make_config(queue={"retry_initial_s": 30, "retry_max_s": 900})
    summary_model = FakeModel([text("Title: Morning briefing\nSummary: Sunny, Dune at 50%.")])
    fact_model = FakeModel()
    embedder = FakeEmbedder(default=[1.0])
    summaries = Summaries(cfg.memory, cfg.queue, sessions, summary_model, embedder)
    facts = FactStore(cfg.memory, cfg.queue, sessions, embedder)
    sweeper = make_sweeper(
        cfg,
        sessions,
        summaries,
        queue=QuietQueue(),
        jobs=[
            FactExtractionJob(
                cfg.memory,
                cfg.queue,
                sessions,
                fact_model,
                facts,
                input_max_chars=cfg.memory.summary_input_max_chars,
            )
        ],
    )
    history = History(sessions, idle_after=SIX_HOURS, on_archive=summaries.changed.set)
    old = await wrote(sessions, "diogo", datetime.now(UTC) - timedelta(hours=25), kind="reminder")
    await history.append("diogo", "reminder", "brief me")
    assert await ended(sessions, old) == "unattended"

    task = asyncio.create_task(sweeper.run_forever())
    try:

        async def finished() -> bool:
            async with sessions() as session:
                row = await session.get(Conversation, old)
            assert row is not None
            return row.summary is not None and row.facts_outcome == "done"

        await until(finished)
    finally:
        task.cancel()
    assert len(summary_model.requests) == 1
    assert fact_model.requests == []


async def test_a_waiting_turn_holds_idle_close_until_it_expires(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A turn waiting for its user's answer is busy (change add-user-input-requests)."""
    from alfred.kernel.questions import Option, Question
    from alfred.kernel.users import Users
    from alfred.turns.inputs.store import InputRequests
    from tests.helpers import ANA, DIOGO

    clock = Clock()
    idle = closer(sessions, clock)
    cid = await wrote(sessions, "ana", clock.now - timedelta(hours=7))
    item = await add_queue_item(sessions, user="ana")
    requests = InputRequests(sessions, History(sessions), Users((DIOGO, ANA)))
    await requests.park(
        item, "ana", tool="t", arguments={}, question=Question("?", (Option("Yes", True),)),
        state={}, turn={}, expires_at=timedelta(minutes=-1),
    )  # fmt: skip
    assert await idle._close_once() == 60
    assert await ended(sessions, cid) is None
    await requests.expire_due()  # the expiry loop ends it
    await idle._close_once()
    assert await ended(sessions, cid) == "idle"
