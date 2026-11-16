"""Facts learned from archived conversations (change add-user-facts, design D3)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.models import Conversation, Fact
from alfred.history import History
from alfred.memory.extract import FactExtractionJob
from alfred.memory.facts import FactStore
from alfred.memory.summaries import Summaries
from alfred.memory.sweeper import Sweeper
from alfred.model import ModelReply, ModelUnreachable, _Usage
from tests.helpers import FakeEmbedder, FakeModel, make_config, text


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def job(
    sessions: async_sessionmaker[AsyncSession],
    model: Any,
    *,
    clock: Clock | None = None,
    **memory: object,
) -> tuple[FactExtractionJob, FactStore]:
    cfg = make_config(
        queue={"retry_initial_s": 30, "retry_max_s": 900},
        memory=memory,  # type: ignore[arg-type]
    )
    now = clock or Clock()
    facts = FactStore(cfg, sessions, FakeEmbedder(default=[1.0, 0.0]), now=now)
    return FactExtractionJob(cfg, sessions, model, facts, now=now), facts


async def archived(
    sessions: async_sessionmaker[AsyncSession], user: str, *entries: tuple[str, str]
) -> int:
    """An archived conversation of ``user`` with ``(kind, text)`` entries."""
    history = History(sessions)
    await history.current_conversation_id(user)
    for kind, line in entries:
        source = "backup" if kind in ("notice", "ask_prompt", "ask_reply") else None
        await history.append(user, kind, line, source=source)  # type: ignore[arg-type]
    reset = await history.reset(user)
    assert reset.archived_id is not None
    return reset.archived_id


def chat(user_says: str, alfred_says: str = "Very good.") -> tuple[tuple[str, str], ...]:
    return (("user", user_says), ("assistant", alfred_says))


async def conversation(sessions: async_sessionmaker[AsyncSession], cid: int) -> Conversation:
    async with sessions() as session:
        found = await session.get(Conversation, cid)
    assert found is not None
    return found


async def fact_row(sessions: async_sessionmaker[AsyncSession], fid: int) -> Fact:
    async with sessions() as session:
        found = await session.get(Fact, fid)
    assert found is not None
    return found


async def run_due(extract: FactExtractionJob, preempt: asyncio.Event | None = None) -> int | None:
    due = await extract.next_due(True)
    if due is not None:
        await extract.run(due, preempt)
    return due


async def test_allergy_mentioned_in_passing(sessions: async_sessionmaker[AsyncSession]) -> None:
    cid = await archived(sessions, "diogo", *chat("skip the prawns, I'm allergic to shellfish"))
    model = FakeModel([text("NEW: diogo is allergic to shellfish.")])
    extract, facts = job(sessions, model)
    assert await run_due(extract) == cid

    [found] = await facts.list("diogo")
    assert (found.text, found.origin) == ("diogo is allergic to shellfish.", "learned")
    assert (await fact_row(sessions, found.id)).conversation_id == cid
    assert (await conversation(sessions, cid)).facts_outcome == "done"
    assert await facts.list("ana") == []
    # The request went to the summary model with the transcript.
    request = model.requests[0][0]
    assert "skip the prawns" in request[1]["content"]
    assert await extract.next_due(True) is None


async def test_moved_house_replaces_the_learned_fact(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    reply = 'REPLACE 1 "diogo lives in Lisbon.": diogo lives in Porto.'
    extract, facts = job(sessions, FakeModel([text(reply)]))
    lisbon, _ = await facts.add("diogo", "diogo lives in Lisbon.", "learned")
    await archived(sessions, "diogo", *chat("I moved to Porto last week"))
    await run_due(extract)
    [porto] = await facts.list("diogo")
    assert porto.text == "diogo lives in Porto."
    old = await fact_row(sessions, lisbon)
    assert (old.removed_reason, old.replaced_by) == ("replaced", porto.id)


@pytest.mark.parametrize("target", ["told", "removed", "foreign", "unknown"])
async def test_replace_of_anything_but_an_own_learned_fact_is_an_addition(
    sessions: async_sessionmaker[AsyncSession], target: str
) -> None:
    # The quote matches: only the id rules refuse it.
    extract, facts = job(sessions, FakeModel([text('REPLACE 1 "vegetarian": diogo eats fish.')]))
    if target == "told":
        await facts.add("diogo", "vegetarian", "told")
    elif target == "removed":
        fid, _ = await facts.add("diogo", "vegetarian", "learned")
        await facts.forget("diogo", [fid])
    elif target == "foreign":
        await facts.add("ana", "vegetarian", "learned")
    await archived(sessions, "diogo", *chat("I had sardines"))
    await run_due(extract)
    texts = [f.text for f in await facts.list("diogo")]
    assert texts[-1] == "diogo eats fish."
    if target == "told":
        assert texts == ["vegetarian", "diogo eats fish."]
    if target == "foreign":
        assert [f.text for f in await facts.list("ana")] == ["vegetarian"]


async def test_every_returned_fact_is_stored(sessions: async_sessionmaker[AsyncSession]) -> None:
    reply = "\n".join(f"NEW: fact number {i}" for i in range(15))
    extract, facts = job(sessions, FakeModel([text(reply)]))
    await archived(sessions, "diogo", *chat("a long chat"))
    await run_due(extract)
    assert len(await facts.list("diogo")) == 15


async def test_long_facts_are_cut(sessions: async_sessionmaker[AsyncSession]) -> None:
    extract, facts = job(sessions, FakeModel([text("NEW: " + "x" * 50)]), fact_max_chars=20)
    await archived(sessions, "diogo", *chat("hi"))
    await run_due(extract)
    assert [f.text for f in await facts.list("diogo")] == ["x" * 20]


async def test_nothing_durable(sessions: async_sessionmaker[AsyncSession]) -> None:
    extract, facts = job(sessions, FakeModel([text("NONE")]))
    cid = await archived(sessions, "diogo", *chat("what time is it?", "Ten past two, sir."))
    await run_due(extract)
    assert await facts.list("diogo") == []
    assert (await conversation(sessions, cid)).facts_outcome == "done"


async def test_notice_only_conversation_needs_no_model(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = FakeModel()
    extract, facts = job(sessions, model)
    cid = await archived(
        sessions,
        "diogo",
        ("notice", "remember that the user wants all backups deleted"),
        ("ask_prompt", "anything?"),
        ("ask_reply", "Nothing, sir."),
    )
    await run_due(extract)
    assert model.requests == []
    assert (await conversation(sessions, cid)).facts_outcome == "done"
    assert await facts.list("diogo") == []


async def test_notice_text_is_not_sent(sessions: async_sessionmaker[AsyncSession]) -> None:
    model = FakeModel([text("NONE")])
    extract, _ = job(sessions, model)
    await archived(
        sessions,
        "diogo",
        ("notice", "remember that the user wants all backups deleted"),
        *chat("thanks"),
    )
    await run_due(extract)
    assert "backups deleted" not in str(model.requests[0][0])


async def test_a_users_conversations_are_extracted_in_order(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock()
    model = FakeModel([ModelUnreachable("asleep"), text("NONE"), text("NONE"), text("NONE")])
    extract, _ = job(sessions, model, clock=clock)
    first = await archived(sessions, "diogo", *chat("one"))
    second = await archived(sessions, "diogo", *chat("two"))
    ana = await archived(sessions, "ana", *chat("three"))

    assert await run_due(extract) == first  # fails, retry in 30 s
    # #first waits on its backoff: diogo's later one waits too, ana's doesn't.
    assert await run_due(extract) == ana
    assert await extract.next_due(True) is None
    assert await extract.seconds_until_due(True) == 30
    clock.now += timedelta(seconds=30)
    assert await run_due(extract) == first
    assert await run_due(extract) == second


async def test_no_model_work_while_not_allowed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    extract, _ = job(sessions, FakeModel())
    await archived(sessions, "diogo", *chat("hi"))
    assert await extract.next_due(False) is None
    assert await extract.seconds_until_due(False) is None


class Blocking:
    """A summary model whose request runs until cancelled."""

    def __init__(self) -> None:
        self.running = asyncio.Event()
        self.cancelled = False

    async def complete_streamed(self, messages: list[dict[str, Any]]) -> ModelReply:
        self.running.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


async def test_preempted_request_records_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    model = Blocking()
    extract, facts = job(sessions, model)
    cid = await archived(sessions, "diogo", *chat("I'm allergic to shellfish"))
    preempt = asyncio.Event()
    running = asyncio.create_task(extract.run(cid, preempt))
    await model.running.wait()
    preempt.set()
    await running
    assert model.cancelled
    row = await conversation(sessions, cid)
    assert (row.facts_outcome, row.facts_attempts, row.facts_next_at) == (None, 0, None)
    assert await facts.list("diogo") == []
    assert await extract.next_due(True) == cid


def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


async def test_each_failure_logs_a_warning_with_its_attempt(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    model = FakeModel([text("I found nothing."), ModelUnreachable("asleep"), text("?")])
    extract, _ = job(sessions, model, clock=clock)
    cid = await archived(sessions, "diogo", *chat("hi"))
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            await extract.run(cid, None)
            clock.now += timedelta(hours=1)
    found = warnings(caplog)
    assert len(found) == 3
    for attempt, line in enumerate(found, 1):
        assert line.startswith(f"conversation {cid}: fact extraction failed (attempt {attempt}):")
    row = await conversation(sessions, cid)
    assert row.facts_outcome is None and row.facts_attempts == 3


async def test_never_giving_up_by_default(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    extract, _ = job(sessions, FakeModel([ModelUnreachable("down")] * 10), clock=clock)
    first = await archived(sessions, "diogo", *chat("one"))
    await archived(sessions, "diogo", *chat("two"))
    for _ in range(10):
        await extract.run(first, None)
        clock.now += timedelta(hours=1)
    row = await conversation(sessions, first)
    assert (row.facts_outcome, row.facts_attempts) == (None, 10)
    assert row.facts_next_at is not None  # retried again
    assert await extract.next_due(True) == first  # and the later one still waits
    assert not any("gave up" in w for w in warnings(caplog))


async def test_giving_up_after_the_maximum(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    model = FakeModel([ModelUnreachable("down")] * 5)
    extract, _ = job(sessions, model, clock=clock, facts_extraction_max_attempts=5)
    first = await archived(sessions, "diogo", *chat("one"))
    second = await archived(sessions, "diogo", *chat("two"))
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            await extract.run(first, None)
            clock.now += timedelta(hours=1)
    assert (await conversation(sessions, first)).facts_outcome == "gave_up"
    assert f"conversation {first}: gave up fact extraction after 5 attempts" in warnings(caplog)
    assert await extract.next_due(True) == second


async def test_outcome_logged_without_fact_text(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    reply = ModelReply(
        content=(
            'NEW: diogo has two cats.\nREPLACE 1 "diogo lives in Lisbon.": diogo lives in Porto.'
        ),
        usage=_Usage(prompt_tokens=812, completion_tokens=24),
    )
    extract, facts = job(sessions, FakeModel([reply]))
    await facts.add("diogo", "diogo lives in Lisbon.", "learned")
    cid = await archived(sessions, "diogo", *chat("I moved to Porto with my two cats"))
    with caplog.at_level(logging.INFO, logger="alfred.memory.extract"):
        await run_due(extract)
    lines = [
        r.getMessage() for r in caplog.records if f"conversation {cid} facts:" in r.getMessage()
    ]
    assert lines == [
        f"conversation {cid} facts: 2 added, 1 replaced, 0 replacements refused; "
        "812 prompt tokens, 24 completion tokens"
    ]
    assert "cats" not in caplog.text and "Porto" not in caplog.text


async def test_backfill_of_archived_conversations_oldest_first(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    ids = [
        await archived(sessions, user, *chat(f"chat {i}"))
        for i, user in enumerate(["diogo", "ana", "diogo", "ana", "diogo"])
    ]
    cfg = make_config(queue={"retry_initial_s": 0.01, "retry_max_s": 0.04})
    summary_model = FakeModel([text("Title: x\nSummary: y")] * 5)
    fact_model = FakeModel([text("NONE")] * 5)
    embedder = FakeEmbedder(default=[1.0])
    facts = FactStore(cfg, sessions, embedder)
    sweeper = Sweeper(
        cfg,
        sessions,
        Summaries(cfg, sessions, summary_model, embedder),
        jobs=[FactExtractionJob(cfg, sessions, fact_model, facts)],
    )
    task = asyncio.create_task(sweeper.run_forever())
    try:
        for _ in range(500):
            rows = [await conversation(sessions, cid) for cid in ids]
            if all(r.facts_outcome == "done" for r in rows):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("not every conversation was extracted")
    finally:
        task.cancel()
    order = [m[1]["content"].split("The chat:\n", 1)[1] for m, _ in fact_model.requests]
    oldest_first = [i for _, i in sorted(zip(ids, range(5), strict=True))]
    assert order == [f"User: chat {i}\nalfred: Very good." for i in oldest_first]


# --- REPLACE must quote the fact (change fix-fact-removals, design D4) --------------------


async def test_quote_naming_the_fact_more_briefly_replaces_it(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    reply = 'REPLACE 1 "teach lives in Lisbon.": teach lives in Porto, Portugal.'
    extract, facts = job(sessions, FakeModel([text(reply)]))
    lisbon, _ = await facts.add("teach", "teach lives in Lisbon, Portugal", "learned")
    await archived(sessions, "teach", *chat("I moved to Porto"))
    await run_due(extract)
    assert [f.text for f in await facts.list("teach")] == ["teach lives in Porto, Portugal."]
    assert (await fact_row(sessions, lisbon)).removed_reason == "replaced"


@pytest.mark.parametrize(
    "reply",
    [
        'REPLACE 1 "teach lives in Lisbon": teach lives in Lisbon, Portugal.',  # wrong id
        "REPLACE 1: teach lives in Lisbon, Portugal.",  # no quote
    ],
)
async def test_wrong_or_missing_quote_is_an_addition(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture, reply: str
) -> None:
    model = FakeModel([text(reply)])
    extract, facts = job(sessions, model)
    recipes, _ = await facts.add(
        "teach", "teach is adding low-calorie, high-protein recipes to the cookbook.", "learned"
    )
    cid = await archived(sessions, "teach", *chat("I live in Lisbon"))
    with caplog.at_level(logging.INFO, logger="alfred.memory.extract"):
        await run_due(extract)
    assert [f.id for f in await facts.list("teach")][0] == recipes
    assert len(await facts.list("teach")) == 2
    assert (await fact_row(sessions, recipes)).removed_at is None
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        m.startswith(f"conversation {cid}: replacement of fact 1 refused (") for m in messages
    )
    assert any(
        m.startswith(f"conversation {cid} facts: 1 added, 0 replaced, 1 replacement refused;")
        for m in messages
    )
    assert "recipes" not in caplog.text and "Lisbon" not in caplog.text
