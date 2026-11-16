from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.model import ModelReply, ModelUnreachable
from alfred.db.models import Conversation, Fact, QueueItem
from alfred.history import History
from alfred.memory.duplicates import FactDuplicateJob
from alfred.memory.embedding import _EmbeddingUnreachable
from alfred.memory.facts import FactEmbeddingJob, FactStore
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


async def stop(*tasks: asyncio.Task[None]) -> None:
    """Cancel background tasks and wait for them, before the fixtures tear down.

    Left to the event loop's own teardown, a task cancelled mid-query rolls
    back on an engine the ``engine`` fixture has already disposed, and the
    test run hangs.
    """
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


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
        await stop(task)
    # Oldest first.
    assert (await row(sessions, first)).title == "Chicken thighs recipe"
    assert (await row(sessions, second)).title == "Disk"


async def test_current_conversation_left_alone(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await History(sessions).append("diogo", "user", "still talking")
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
        async def send(self, user: object, text_: str) -> None:
            sent.append(text_)

    agent = make_agent(cfg, FakeModel([text("one"), text("two")]), history, sessions, _no_mcp)
    worker = Worker(
        queue, agent, history, Outbox(), cfg.queue, users=cfg.users, nudge=mem.summaries.changed.set
    )
    tasks = [asyncio.create_task(mem.run_forever()), asyncio.create_task(worker.run_forever())]
    try:
        await until(lambda: _has_failed(summary_model, 3))
        await queue.enqueue_user("diogo", "first")
        await queue.enqueue_user("diogo", "second")

        async def answered() -> bool:
            return sent == ["one", "two"]

        await until(answered)
        assert len(summary_model.requests) >= 3  # still failing in the background
    finally:
        await stop(*tasks)


async def _has_failed(model: FakeModel, times: int) -> bool:
    return len(model.requests) >= times


def _no_mcp(server, timeout):  # type: ignore[no-untyped-def]
    raise AssertionError("no MCP servers are configured")


# --- waiting for a quiet queue (change close-idle-conversations, D5, D6) ----------


class Ticks:
    """The queue's monotonic clock, moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


BLOCK = object()


class ScriptedStream:
    """A summary model: ``BLOCK`` waits until cancelled, a reply is returned.

    Records each request's start and whether it was cancelled.
    """

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.started = 0
        self.cancelled = 0
        self.running = asyncio.Event()

    async def complete_streamed(self, messages: list[dict[str, Any]]) -> ModelReply:
        self.started += 1
        step = self.script.pop(0)
        if step is BLOCK:
            self.running.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                self.running.clear()
                raise
        assert isinstance(step, ModelReply)
        return step


def quiet_sweeper(
    sessions: async_sessionmaker[AsyncSession],
    model: object,
    embedder: FakeEmbedder,
    quiet_s: float,
) -> tuple[Sweeper, Queue, Ticks]:
    cfg = make_config(memory={"summary_quiet_s": quiet_s})
    ticks = Ticks()
    queue = Queue(sessions, clock=ticks)
    summaries = Summaries(cfg, sessions, model, embedder)  # type: ignore[arg-type]
    return Sweeper(cfg, sessions, summaries, queue=queue), queue, ticks


async def finish_all(sessions: async_sessionmaker[AsyncSession], queue: Queue) -> None:
    """What the worker does when its items end."""
    async with sessions.begin() as session:
        await session.execute(update(QueueItem).values(status="done"))
    queue.activity.finished()


async def settle() -> None:
    """Let the sweeper run as far as it can."""
    for _ in range(20):
        await asyncio.sleep(0.01)


async def test_no_summary_while_an_item_is_pending(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    cid = await archived(sessions, "chicken?", "Lemon.")
    model = FakeModel([text(REPLY)])
    mem, queue, ticks = quiet_sweeper(sessions, model, FakeEmbedder(default=[1.0]), 0)
    await queue.enqueue_ask("backup", "anything?", "diogo")
    task = asyncio.create_task(mem.run_forever())
    try:
        await settle()
        assert model.requests == []
        await finish_all(sessions, queue)
        await until(lambda: _summarised(sessions, cid))
    finally:
        await stop(task)


async def test_no_summary_within_the_quiet_period(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    cid = await archived(sessions, "chicken?", "Lemon.")
    model = FakeModel([text(REPLY)])
    mem, queue, ticks = quiet_sweeper(sessions, model, FakeEmbedder(default=[1.0]), 120)
    queue.activity.finished()  # an item just ended
    task = asyncio.create_task(mem.run_forever())
    try:
        ticks.now += 119
        mem.summaries.changed.set()  # wake it: still within the period
        await settle()
        assert model.requests == []
        ticks.now += 1
        mem.summaries.changed.set()
        await until(lambda: _summarised(sessions, cid))
    finally:
        await stop(task)


async def test_an_arriving_item_cancels_the_summary(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    cid = await archived(sessions, "chicken?", "Lemon.")
    model = ScriptedStream(BLOCK, BLOCK, text(REPLY))
    mem, queue, ticks = quiet_sweeper(sessions, model, FakeEmbedder(default=[1.0]), 60)
    ticks.now += 60  # quiet since start
    task = asyncio.create_task(mem.run_forever())
    try:
        for cancelled in (1, 2):
            await asyncio.wait_for(model.running.wait(), 5)
            await queue.enqueue_user("diogo", "a message")
            await until(lambda: _true(model.cancelled == cancelled))  # noqa: B023
            stored = await row(sessions, cid)
            assert stored.summary is None
            assert stored.summary_attempts == 0 and stored.summary_next_at is None
            # The item is answered; the summary waits for another quiet period.
            await finish_all(sessions, queue)
            await settle()
            assert model.started == cancelled
            ticks.now += 60
            mem.summaries.changed.set()
        await until(lambda: _summarised(sessions, cid))
        assert model.started == 3
        assert (await row(sessions, cid)).summary_attempts == 0
    finally:
        await stop(task)


async def test_embedding_only_does_not_wait_for_the_queue(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    needs_summary = await archived(sessions, "a", "b")
    needs_embedding = await archived(sessions, "chicken?", "Lemon.")
    async with sessions.begin() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == needs_embedding)
            .values(title="Chicken", summary="Lemon and garlic.")
        )
    model = FakeModel()
    embedder = FakeEmbedder(default=[1.0])
    mem, queue, _ = quiet_sweeper(sessions, model, embedder, 0)
    await queue.enqueue_user("diogo", "busy")
    task = asyncio.create_task(mem.run_forever())
    try:

        async def embedded() -> bool:
            return (await row(sessions, needs_embedding)).embedding is not None

        await until(embedded)
        assert model.requests == []
        assert (await row(sessions, needs_summary)).summary is None
    finally:
        await stop(task)


async def _summarised(sessions: async_sessionmaker[AsyncSession], cid: int) -> bool:
    stored = await row(sessions, cid)
    return stored.summary is not None and stored.embedding is not None


async def _true(value: bool) -> bool:
    return value


# --- a loop over jobs (change add-user-facts, design D2) ----------------------------


class FakeJob:
    """A job with keys due by hand; records what it ran and the preempt it got."""

    def __init__(self, name: str, *, uses_model: bool) -> None:
        self.name = name
        self.uses_model = uses_model
        self.changed = asyncio.Event()
        self.due: list[str] = []
        self.later: float | None = None
        self.ran: list[tuple[str, asyncio.Event | None]] = []
        self.asked: list[bool] = []

    async def next_due(self, model_allowed: bool) -> str | None:
        self.asked.append(model_allowed)
        if self.uses_model and not model_allowed:
            return None
        return self.due[0] if self.due else None

    async def seconds_until_due(self, model_allowed: bool) -> float | None:
        return self.later

    async def run(self, key: str, preempt: asyncio.Event | None) -> None:
        self.ran.append((key, preempt))
        self.due.remove(key)


def job_sweeper(
    sessions: async_sessionmaker[AsyncSession], *jobs: FakeJob, quiet_s: float = 0
) -> tuple[Sweeper, Queue, Ticks]:
    cfg = make_config(memory={"summary_quiet_s": quiet_s})
    ticks = Ticks()
    queue = Queue(sessions, clock=ticks)
    summaries = Summaries(cfg, sessions, FakeModel(), FakeEmbedder())
    return Sweeper(cfg, sessions, summaries, queue=queue, jobs=jobs), queue, ticks


async def test_first_due_job_runs_in_priority_order(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first, second = FakeJob("first", uses_model=True), FakeJob("second", uses_model=False)
    first.due, second.due = ["a1", "a2"], ["b1"]
    mem, queue, _ = job_sweeper(sessions, first, second)
    task = asyncio.create_task(mem.run_forever())
    try:
        await until(_lists_empty(first.due, second.due))
    finally:
        await stop(task)
    assert [k for k, _ in first.ran] == ["a1", "a2"]
    assert [k for k, _ in second.ran] == ["b1"]
    # The model job got the queue's arrival event as preempt; the other none.
    assert all(p is queue.activity.arrived for _, p in first.ran)
    assert second.ran[0][1] is None


async def test_model_job_waits_for_a_quiet_queue(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model_job, other = FakeJob("model", uses_model=True), FakeJob("other", uses_model=False)
    model_job.due = ["m1"]
    mem, queue, _ = job_sweeper(sessions, model_job, other)
    await queue.enqueue_ask("backup", "anything?", "diogo")
    task = asyncio.create_task(mem.run_forever())
    try:
        await settle()
        assert model_job.ran == []
        assert model_job.asked and not any(model_job.asked)  # never offered the model
        # Work that needs no model still runs while the item is pending.
        other.due = ["o1"]
        other.changed.set()
        await until(_lists_empty(other.due))
        await finish_all(sessions, queue)
        await until(_lists_empty(model_job.due))
    finally:
        await stop(task)


async def test_sleeps_until_the_soonest_job_and_wakes_on_either(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first, second = FakeJob("first", uses_model=False), FakeJob("second", uses_model=False)
    first.later, second.later = 3600, 0.05
    mem, _, _ = job_sweeper(sessions, first, second)
    task = asyncio.create_task(mem.run_forever())
    try:
        await settle()
        asked = len(second.asked)
        await asyncio.sleep(0.2)  # second's 0.05 s passed: the sweeper looked again
        assert len(second.asked) > asked
        second.later = None
        first.later = None
        await settle()
        # Woken by either job's changed.
        first.due = ["a"]
        first.changed.set()
        await until(_lists_empty(first.due))
        second.due = ["b"]
        second.changed.set()
        await until(_lists_empty(second.due))
    finally:
        await stop(task)


def _lists_empty(*lists: list[str]):  # type: ignore[no-untyped-def]
    async def check() -> bool:
        return not any(lists)

    return check


# --- fact embedding (change add-user-facts, design D6) ---------------------------------


def fact_sweeper(
    sessions: async_sessionmaker[AsyncSession],
    embedder: FakeEmbedder,
    *,
    queue: Queue | None = None,
    model: FakeModel | None = None,
) -> tuple[Sweeper, FactStore]:
    """Fact embedding, then the duplicate check with ``model``, as ``serve`` runs them."""
    cfg = make_config(queue=FAST)
    facts = FactStore(cfg, sessions, embedder)
    summaries = Summaries(cfg, sessions, FakeModel(), embedder)
    jobs = [FactEmbeddingJob(facts), FactDuplicateJob(facts, model or FakeModel())]
    return Sweeper(cfg, sessions, summaries, queue=queue, jobs=jobs), facts


async def _fact_row(sessions: async_sessionmaker[AsyncSession], fact_id: int) -> Fact:
    async with sessions() as session:
        found = await session.get(Fact, fact_id)
    assert found is not None
    return found


async def test_fact_added_while_ragman_is_down_is_embedded_later(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    down = _EmbeddingUnreachable("down")
    embedder = FakeEmbedder([[1.0, 0.0], down, down], default=[0.99, 0.05])
    model = FakeModel([text("SAME 1")])
    mem, facts = fact_sweeper(sessions, embedder, model=model)
    old, _ = await facts.add("diogo", "allergic to shellfish", "learned")
    await facts.embed(old)
    check = await facts.duplicate_candidates(old)
    assert check is not None
    await facts.finish_duplicate_check(check, [])  # alone when it was stored
    new, _ = await facts.add("diogo", "the user is allergic to shellfish", "learned")
    await facts.embed(new)  # ragman down
    assert (await _fact_row(sessions, new)).embedding is None
    task = asyncio.create_task(mem.run_forever())
    try:
        # Embedded once ragman answers, then checked for duplicates by the model.
        async def deduped() -> bool:
            return (await _fact_row(sessions, new)).removed_reason == "duplicate"

        await until(deduped)
    finally:
        await stop(task)
    assert (await _fact_row(sessions, new)).replaced_by == old
    assert len(model.requests) == 1


async def test_changing_the_embedding_model_reembeds_facts(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, old_facts = fact_sweeper(sessions, FakeEmbedder(default=[1.0, 0.0], model="old-model"))
    fid, _ = await old_facts.add("diogo", "runs a homelab", "told")
    await old_facts.embed(fid)
    embedder = FakeEmbedder(default=[0.0, 1.0, 0.0], model="new-model")
    mem, _ = fact_sweeper(sessions, embedder)
    assert await mem.sweep_once() is None
    assert embedder.documents == ["runs a homelab"]
    stored = await _fact_row(sessions, fid)
    assert stored.embedding_model == "new-model" and len(stored.embedding) == 3


async def test_facts_are_embedded_while_an_item_is_pending(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    queue = Queue(sessions)
    mem, facts = fact_sweeper(sessions, FakeEmbedder(default=[1.0, 0.0]), queue=queue)
    await queue.enqueue_ask("backup", "anything?", "diogo")
    task = asyncio.create_task(mem.run_forever())
    try:
        fid, _ = await facts.add("diogo", "my car is electric", "told")

        async def embedded() -> bool:
            return (await _fact_row(sessions, fid)).embedding is not None

        await until(embedded)
    finally:
        await stop(task)
