from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.model import ModelUnreachable
from alfred.db.models import Conversation
from alfred.history import History
from alfred.memory.summaries import Summaries
from alfred.memory.sweeper import Sweeper
from alfred.queue import Queue, Worker
from tests.helpers import FakeEmbedder, FakeModel, make_agent, make_config, text
from tests.memory.test_summarise import REPLY, archived, row

FAST = {"retry_initial_s": 0.01, "retry_max_s": 0.04}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def memory(
    sessions: async_sessionmaker[AsyncSession],
    model: FakeModel,
    embedder: FakeEmbedder,
    *,
    queue: dict[str, float] | None = None,
    clock: Clock | None = None,
) -> Sweeper:
    cfg = make_config(queue=queue or {"retry_initial_s": 30, "retry_max_s": 900})
    if clock is None:
        return Sweeper(cfg, sessions, Summaries(cfg, sessions, model, embedder))
    return Sweeper(cfg, sessions, Summaries(cfg, sessions, model, embedder, now=clock), now=clock)


async def until(condition) -> None:  # type: ignore[no-untyped-def]
    for _ in range(500):
        if await condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never became true")


async def test_backfills_conversations_without_summary(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first = await archived(sessions, "chicken?", "Lemon.")
    second = await archived(sessions, "disk?", "Full.")
    model = FakeModel([text(REPLY), text("Title: Disk\nSummary: It was full.")])
    mem = memory(sessions, model, FakeEmbedder(default=[1.0]))

    task = asyncio.create_task(mem.run_forever())
    try:

        async def both_done() -> bool:
            rows = [await row(sessions, first), await row(sessions, second)]
            return all(r.summary is not None and r.embedding is not None for r in rows)

        await until(both_done)
    finally:
        task.cancel()
    # Oldest first.
    assert (await row(sessions, first)).title == "Chicken thighs recipe"
    assert (await row(sessions, second)).title == "Disk"


async def test_current_conversation_left_alone(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await History(sessions).append("owner", "still talking")
    mem = memory(sessions, FakeModel(), FakeEmbedder())
    assert await mem.sweep_once() == 900  # nothing pending: wait the longest delay


async def test_retries_with_backoff(sessions: async_sessionmaker[AsyncSession]) -> None:
    cid = await archived(sessions, "chicken?", "Lemon.")
    clock = Clock()
    model = FakeModel([ModelUnreachable("asleep"), ModelUnreachable("asleep"), text(REPLY)])
    mem = memory(sessions, model, FakeEmbedder(default=[1.0]), clock=clock)

    assert await mem.sweep_once() is None  # attempt 1 fails
    assert await mem.sweep_once() == 30  # not due for 30 s; no model request
    assert len(model.requests) == 1

    clock.now += timedelta(seconds=30)
    assert await mem.sweep_once() is None  # attempt 2 fails
    assert await mem.sweep_once() == 60  # the delay doubled
    stored = await row(sessions, cid)
    assert stored.summary_attempts == 2

    clock.now += timedelta(seconds=60)
    assert await mem.sweep_once() is None  # attempt 3 succeeds
    stored = await row(sessions, cid)
    assert stored.title == "Chicken thighs recipe"
    assert stored.summary_attempts == 0 and stored.summary_next_at is None
    assert await mem.sweep_once() == 900


async def test_one_failing_conversation_does_not_hold_up_others(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    stuck = await archived(sessions, "a", "b")
    other = await archived(sessions, "c", "d")
    clock = Clock()
    model = FakeModel([ModelUnreachable("x"), text(REPLY)])
    mem = memory(sessions, model, FakeEmbedder(default=[1.0]), clock=clock)
    await mem.sweep_once()
    await mem.sweep_once()
    assert (await row(sessions, stuck)).summary is None
    assert (await row(sessions, other)).summary is not None


async def test_reembeds_rows_from_another_model(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    cid = await archived(sessions, "chicken?", "Lemon.")
    async with sessions.begin() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == cid)
            .values(
                title="Chicken thighs recipe",
                summary="Lemon and garlic.",
                embedding=[1.0, 0.0],
                embedding_model="old-model",
            )
        )
    model = FakeModel()
    embedder = FakeEmbedder([[0.0, 1.0, 0.0]], model="new-model")
    mem = memory(sessions, model, embedder)
    assert await mem.sweep_once() is None
    assert model.requests == []  # the summary is kept
    assert embedder.documents == ["Chicken thighs recipe. Lemon and garlic."]
    stored = await row(sessions, cid)
    assert stored.embedding_model == "new-model"
    assert len(stored.embedding) == 3


async def test_nudged_after_reset(sessions: async_sessionmaker[AsyncSession]) -> None:
    mem = memory(sessions, FakeModel([text(REPLY)]), FakeEmbedder(default=[1.0]))
    cid = await archived(sessions, "chicken?", "Lemon.")
    mem.summaries.changed.clear()
    await mem.summaries.summarise(cid)
    assert mem.summaries.changed.is_set()


async def test_owner_messages_answered_while_sweeper_fails(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await archived(sessions, "old", "chat")
    summary_model = FakeModel([ModelUnreachable("down")] * 1000)
    mem = memory(sessions, summary_model, FakeEmbedder(default=[1.0]), queue=FAST)

    cfg = make_config(queue=FAST)
    history = History(sessions)
    queue = Queue(sessions)
    sent: list[str] = []

    class Outbox:
        async def send(self, text_: str) -> None:
            sent.append(text_)

    agent = make_agent(cfg, FakeModel([text("one"), text("two")]), history, sessions, _no_mcp)
    worker = Worker(
        queue, agent, history, Outbox(), cfg.queue, owner_address="sir", memory=mem.summaries
    )
    tasks = [asyncio.create_task(mem.run_forever()), asyncio.create_task(worker.run_forever())]
    try:
        await until(lambda: _has_failed(summary_model, 3))
        await queue.enqueue_owner("first")
        await queue.enqueue_owner("second")

        async def answered() -> bool:
            return sent == ["one", "two"]

        await until(answered)
        assert len(summary_model.requests) >= 3  # still failing in the background
    finally:
        for task in tasks:
            task.cancel()


async def _has_failed(model: FakeModel, times: int) -> bool:
    return len(model.requests) >= times


def _no_mcp(server, timeout):  # type: ignore[no-untyped-def]
    raise AssertionError("no MCP servers are configured")
