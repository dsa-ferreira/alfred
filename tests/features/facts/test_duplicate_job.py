"""The model's duplicate check of new facts (change fix-fact-removals, design D2, D3)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.features.archive.summaries import Summaries
from alfred.features.facts.duplicate_job import FactDuplicateJob
from alfred.features.facts.embedding_job import FactEmbeddingJob
from alfred.features.facts.store import FactStore
from alfred.features.facts.tables import Fact
from alfred.infra.llm.chat import ModelReply, ModelUnreachable, _Usage
from alfred.runtime.turns.queue import Queue
from tests.helpers import FakeEmbedder, FakeModel, make_config, make_sweeper, text

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
CLOSE = [1.0, 0.0]  # every fact embedded the same: all are candidates


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def job(
    sessions: async_sessionmaker[AsyncSession],
    model: Any,
    embedder: FakeEmbedder | None = None,
    clock: Clock | None = None,
) -> tuple[FactDuplicateJob, FactStore]:
    cfg = make_config(queue={"retry_initial_s": 30, "retry_max_s": 900})
    facts = FactStore(
        cfg.memory,
        cfg.queue,
        sessions,
        embedder or FakeEmbedder(default=CLOSE),
        now=clock or Clock(),
    )
    return FactDuplicateJob(facts, model), facts


async def stored(facts: FactStore, user: str, fact_text: str, origin: str = "learned") -> int:
    fid, _ = await facts.add(user, fact_text, origin)  # type: ignore[arg-type]
    assert await facts.embed(fid)
    return fid


async def row(sessions: async_sessionmaker[AsyncSession], fid: int) -> Fact:
    async with sessions() as session:
        found = await session.get(Fact, fid)
    assert found is not None
    return found


async def check(extract: FactDuplicateJob, preempt: asyncio.Event | None = None) -> int | None:
    due = await extract.next_due(True)
    if due is not None:
        await extract.run(due, preempt)
    return due


async def check_all(dupes: FactDuplicateJob) -> None:
    while await check(dupes) is not None:
        pass


async def test_same_fact_in_other_words(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    reply = ModelReply(content="SAME 1", usage=_Usage(prompt_tokens=90, completion_tokens=3))
    model = FakeModel([reply])
    dupes, facts = job(sessions, model)
    old = await stored(facts, "diogo", "diogo is allergic to shellfish.")
    await dupes.run(old, None)  # alone: checked without the model
    new = await stored(facts, "diogo", "the user is allergic to shellfish.")
    with caplog.at_level(logging.INFO):
        assert await check(dupes) == new
    removed = await row(sessions, new)
    assert (removed.removed_reason, removed.replaced_by) == ("duplicate", old)
    assert removed.duplicate_checked_at == NOW
    assert [f.id for f in await facts.list("diogo")] == [old]
    assert f"fact {new} duplicate of {old}, removed {new}" in caplog.text
    assert (
        f"fact {new} duplicate check: 1 compared, 1 duplicate; "
        "90 prompt tokens, 3 completion tokens"
    ) in caplog.text
    assert "shellfish" not in caplog.text
    # The request named the new fact and the candidate.
    user_message = model.requests[0][0][1]["content"]
    assert "the user is allergic" in user_message and f"[{old}] diogo is allergic" in user_message
    assert await dupes.next_due(True) is None


async def test_told_beats_learned(sessions: async_sessionmaker[AsyncSession]) -> None:
    dupes, facts = job(sessions, FakeModel([text("SAME 1")]))
    learned = await stored(facts, "diogo", "prefers metric units")
    await dupes.run(learned, None)
    told = await stored(facts, "diogo", "always use metric units", "told")
    await check(dupes)
    assert (await row(sessions, learned)).replaced_by == told
    assert [f.id for f in await facts.list("diogo")] == [told]


async def test_close_but_different_both_stay(sessions: async_sessionmaker[AsyncSession]) -> None:
    # Each new fact is checked: the first against the second, then the reverse.
    model = FakeModel([text("NONE"), text("NONE")])
    dupes, facts = job(sessions, model)
    await stored(facts, "ana", "ana likes Teach very much.")
    await stored(facts, "ana", "ana has a person she refers to as Teach.")
    await check_all(dupes)
    assert len(await facts.list("ana")) == 2
    rows = [await row(sessions, f.id) for f in await facts.list("ana")]
    assert all(r.duplicate_checked_at is not None for r in rows)
    assert len(model.requests) == 2


async def test_nothing_close_needs_no_model(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel()
    dupes, facts = job(sessions, model, FakeEmbedder([[1.0, 0.0], [0.0, 1.0]]))
    await stored(facts, "diogo", "allergic to shellfish")
    await stored(facts, "diogo", "runs a homelab")
    await check_all(dupes)
    assert model.requests == []
    assert len(await facts.list("diogo")) == 2


async def test_other_users_not_compared(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel()
    dupes, facts = job(sessions, model)
    await stored(facts, "diogo", "allergic to shellfish")
    await stored(facts, "ana", "allergic to shellfish")
    await check_all(dupes)
    assert model.requests == []


async def test_ids_that_arent_candidates_are_ignored(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    dupes, facts = job(
        sessions,
        FakeModel([text("SAME 2\nSAME 99")]),
        FakeEmbedder([[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]]),
    )
    first = await stored(facts, "diogo", "runs a homelab")
    far = await stored(facts, "diogo", "likes jazz")  # id 2, not close
    for fid in (first, far):
        await dupes.run(fid, None)
    new = await stored(facts, "diogo", "runs a home lab")
    await check(dupes)
    assert len(await facts.list("diogo")) == 3
    assert (await row(sessions, new)).duplicate_checked_at is not None


async def test_fact_removed_meanwhile_is_skipped(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel()
    dupes, facts = job(sessions, model)
    await stored(facts, "diogo", "allergic to shellfish")
    new = await stored(facts, "diogo", "is allergic to shellfish")
    await facts.forget("diogo", [new])
    await dupes.run(new, None)
    assert model.requests == []
    assert (await row(sessions, new)).removed_reason == "forgotten"


class Blocking:
    def __init__(self) -> None:
        self.running = asyncio.Event()

    async def complete_streamed(self, messages: list[dict[str, Any]]) -> ModelReply:
        self.running.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_preempted_check_records_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = Blocking()
    dupes, facts = job(sessions, model)
    old = await stored(facts, "diogo", "allergic to shellfish")
    await dupes.run(old, None)  # alone: no request
    new = await stored(facts, "diogo", "is allergic to shellfish")
    preempt = asyncio.Event()
    running = asyncio.create_task(dupes.run(new, preempt))
    await model.running.wait()
    preempt.set()
    await running
    fresh = await row(sessions, new)
    assert (fresh.duplicate_checked_at, fresh.duplicate_attempts, fresh.removed_at) == (
        None,
        0,
        None,
    )
    assert await dupes.next_due(True) == new


async def test_failures_retry_and_keep_the_fact(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    model = FakeModel([text("They differ."), ModelUnreachable("asleep")])
    dupes, facts = job(sessions, model, clock=clock)
    old = await stored(facts, "diogo", "allergic to shellfish")
    await dupes.run(old, None)
    new = await stored(facts, "diogo", "is allergic to shellfish")
    with caplog.at_level(logging.WARNING):
        await check(dupes)
        assert await dupes.next_due(True) is None  # waiting 30 s
        assert await dupes.seconds_until_due(True) == 30
        clock.now += timedelta(seconds=30)
        await check(dupes)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings[0].startswith(f"fact {new}: duplicate check failed (attempt 1)")
    assert warnings[1].startswith(f"fact {new}: duplicate check failed (attempt 2)")
    assert "shellfish" not in caplog.text
    fresh = await row(sessions, new)
    assert fresh.duplicate_attempts == 2 and fresh.duplicate_checked_at is None
    assert fresh.duplicate_next_at == clock.now + timedelta(seconds=60)
    assert [f.id for f in await facts.list("diogo")] == [old, new]


async def test_not_offered_without_the_model(sessions: async_sessionmaker[AsyncSession]) -> None:
    dupes, facts = job(sessions, FakeModel())
    await stored(facts, "diogo", "x")
    assert await dupes.next_due(False) is None
    assert await dupes.seconds_until_due(False) is None


async def test_unembedded_fact_waits(sessions: async_sessionmaker[AsyncSession]) -> None:
    dupes, facts = job(sessions, FakeModel())
    await facts.add("diogo", "not embedded yet", "told")
    assert await dupes.next_due(True) is None


async def test_sweeper_checks_only_while_the_queue_is_quiet(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    cfg = make_config(memory={"summary_quiet_s": 0})
    queue = Queue(sessions)
    embedder = FakeEmbedder(default=CLOSE)
    facts = FactStore(cfg.memory, cfg.queue, sessions, embedder)
    model = FakeModel([text("SAME 1")])
    sweeper = make_sweeper(
        cfg,
        sessions,
        Summaries(cfg.memory, cfg.queue, sessions, FakeModel(), embedder),
        queue=queue,
        jobs=[FactEmbeddingJob(facts), FactDuplicateJob(facts, model)],
    )
    old, _ = await facts.add("diogo", "allergic to shellfish", "learned")
    await facts.embed(old)
    await facts.finish_duplicate_check(  # already checked, like a fact from before
        (await facts.duplicate_candidates(old)),
        [],  # type: ignore[arg-type]
    )
    await queue.enqueue_ask("backup", "anything?", "diogo")
    task = asyncio.create_task(sweeper.run_forever())
    try:
        new, _ = await facts.add("diogo", "is allergic to shellfish", "learned")

        async def embedded() -> bool:
            return (await row(sessions, new)).embedding is not None

        await _until(embedded)  # embedding doesn't wait for the queue...
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert model.requests == []  # ...the check does
        from sqlalchemy import update

        from alfred.runtime.turns.tables import QueueItem

        async with sessions.begin() as session:
            await session.execute(update(QueueItem).values(status="done"))
        queue.activity.finished()

        async def removed() -> bool:
            return (await row(sessions, new)).removed_reason == "duplicate"

        await _until(removed)
    finally:
        task.cancel()


async def _until(condition) -> None:  # type: ignore[no-untyped-def]
    for _ in range(500):
        if await condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never became true")
