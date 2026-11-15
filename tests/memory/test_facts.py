"""The fact store: per-user facts, soft removal and near-duplicates (change add-user-facts)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.models import Fact
from alfred.memory.embedding import EmbeddingUnreachable
from alfred.memory.facts import Duplicate, FactsNotFound, FactStore
from tests.helpers import FakeEmbedder, make_config

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def store(
    sessions: async_sessionmaker[AsyncSession],
    embedder: FakeEmbedder | None = None,
    **memory: object,
) -> FactStore:
    cfg = make_config(
        queue={"retry_initial_s": 30, "retry_max_s": 900},
        memory=memory,  # type: ignore[arg-type]
    )
    return FactStore(cfg, sessions, embedder or FakeEmbedder(default=[1.0, 0.0]), now=lambda: NOW)


async def fact(sessions: async_sessionmaker[AsyncSession], fact_id: int) -> Fact:
    async with sessions() as session:
        found = await session.get(Fact, fact_id)
    assert found is not None
    return found


async def test_add_and_list_in_id_order(sessions: async_sessionmaker[AsyncSession]) -> None:
    facts = store(sessions)
    a, _ = await facts.add("diogo", "allergic to shellfish", "told")
    b, _ = await facts.add("diogo", "runs a homelab", "learned")
    await facts.add("ana", "likes tea", "told")
    listed = await facts.list("diogo")
    assert [(f.id, f.text, f.origin) for f in listed] == [
        (a, "allergic to shellfish", "told"),
        (b, "runs a homelab", "learned"),
    ]
    assert [f.text for f in await facts.list("ana")] == ["likes tea"]


async def test_learned_fact_keeps_its_conversation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = store(sessions)
    fid, _ = await facts.add("diogo", "x", "learned", None)
    assert (await fact(sessions, fid)).conversation_id is None


async def test_forgotten_fact_is_kept_but_gone(sessions: async_sessionmaker[AsyncSession]) -> None:
    facts = store(sessions)
    fid, _ = await facts.add("diogo", "allergic to shellfish", "told")
    removed, missing = await facts.forget("diogo", [fid])
    assert [f.id for f in removed] == [fid] and missing == []
    row = await fact(sessions, fid)
    assert row.removed_reason == "forgotten" and row.removed_at == NOW
    assert row.replaced_by is None
    assert await facts.list("diogo") == []
    # Forgetting it again: not found.
    assert await facts.forget("diogo", [fid]) == ([], [fid])


async def test_replaced_fact_points_at_its_replacement(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = store(sessions)
    old, _ = await facts.add("diogo", "lives in Lisbon", "told")
    new, replaced = await facts.add("diogo", "lives in Porto", "told", replaces=[old])
    assert [f.id for f in replaced] == [old]
    row = await fact(sessions, old)
    assert (row.removed_reason, row.replaced_by) == ("replaced", new)
    assert [f.id for f in await facts.list("diogo")] == [new]


async def test_add_with_foreign_or_unknown_replaces_changes_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = store(sessions)
    mine, _ = await facts.add("ana", "likes tea", "told")
    theirs, _ = await facts.add("diogo", "lives in Lisbon", "told")
    with pytest.raises(FactsNotFound) as exc:
        await facts.add("ana", "likes coffee", "told", replaces=[mine, theirs, 999])
    assert exc.value.ids == [theirs, 999]
    assert [f.text for f in await facts.list("ana")] == ["likes tea"]
    assert [f.text for f in await facts.list("diogo")] == ["lives in Lisbon"]


async def test_replace_is_for_learned_facts_only(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = store(sessions)
    told, _ = await facts.add("diogo", "vegetarian", "told")
    learned, _ = await facts.add("diogo", "lives in Lisbon", "learned")
    foreign, _ = await facts.add("ana", "lives in Faro", "learned")
    new, _ = await facts.add("diogo", "lives in Porto", "learned")
    assert await facts.replace("diogo", new, [told, learned, foreign, 999]) == [learned]
    assert (await fact(sessions, learned)).replaced_by == new
    assert (await fact(sessions, told)).removed_at is None
    assert (await fact(sessions, foreign)).removed_at is None


async def test_forget_reports_another_users_ids_as_not_found(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = store(sessions)
    theirs, _ = await facts.add("diogo", "allergic to shellfish", "told")
    assert await facts.forget("ana", [theirs, 99]) == ([], [theirs, 99])
    assert (await fact(sessions, theirs)).removed_at is None


async def test_concurrent_forget_and_replace_mark_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = store(sessions)
    target, _ = await facts.add("diogo", "lives in Lisbon", "learned")
    new, _ = await facts.add("diogo", "lives in Porto", "learned")
    forgot, replaced = await asyncio.gather(
        facts.forget("diogo", [target]), facts.replace("diogo", new, [target])
    )
    # Exactly one of them removed it.
    assert (len(forgot[0]), len(replaced)) in {(1, 0), (0, 1)}
    row = await fact(sessions, target)
    assert row.removed_at is not None
    assert (row.removed_reason == "replaced") == bool(replaced)
    # And it stays removed.
    assert target not in [f.id for f in await facts.list("diogo")]


# --- Embedding and near-duplicates (design D6) ---------------------------------------


async def embedded(
    facts: FactStore, user: str, fact_text: str, origin: str = "learned"
) -> tuple[int, Duplicate | None]:
    fid, _ = await facts.add(user, fact_text, origin)  # type: ignore[arg-type]
    return fid, await facts.embed(fid)


async def test_near_duplicate_learned_fact_is_removed(
    sessions: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    embedder = FakeEmbedder([[1.0, 0.0], [0.99, 0.05]])
    facts = store(sessions, embedder)
    old, _ = await embedded(facts, "diogo", "allergic to shellfish")
    with caplog.at_level(logging.INFO, logger="alfred.memory.facts"):
        new, dup = await embedded(facts, "diogo", "the user is allergic to shellfish")
    assert dup == Duplicate(kept=old, removed=new)
    row = await fact(sessions, new)
    assert (row.removed_reason, row.replaced_by) == ("duplicate", old)
    assert row.text == "the user is allergic to shellfish"  # still stored
    assert [f.id for f in await facts.list("diogo")] == [old]
    assert f"fact {new} duplicate of {old}, removed {new}" in caplog.text


async def test_distant_facts_both_stay(sessions: async_sessionmaker[AsyncSession]) -> None:
    facts = store(sessions, FakeEmbedder([[1.0, 0.0], [0.0, 1.0]]))
    await embedded(facts, "diogo", "allergic to shellfish")
    _, dup = await embedded(facts, "diogo", "runs a homelab")
    assert dup is None
    assert len(await facts.list("diogo")) == 2


async def test_removed_fact_is_never_the_match(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = store(sessions, FakeEmbedder(default=[1.0, 0.0]))
    old, _ = await embedded(facts, "diogo", "allergic to shellfish")
    await facts.forget("diogo", [old])
    new, dup = await embedded(facts, "diogo", "allergic to shellfish")
    assert dup is None
    assert [f.id for f in await facts.list("diogo")] == [new]


async def test_told_beats_learned_regardless_of_age(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = store(sessions, FakeEmbedder(default=[1.0, 0.0]))
    told, _ = await embedded(facts, "diogo", "always use metric units", "told")
    learned, dup = await embedded(facts, "diogo", "prefers metric units", "learned")
    assert dup == Duplicate(kept=told, removed=learned)

    facts2 = store(sessions, FakeEmbedder(default=[0.0, 1.0]))
    learned2, _ = await embedded(facts2, "diogo", "likes jazz", "learned")
    told2, dup2 = await embedded(facts2, "diogo", "loves jazz", "told")
    assert dup2 == Duplicate(kept=told2, removed=learned2)
    assert (await fact(sessions, learned2)).replaced_by == told2


async def test_other_users_facts_not_compared(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = store(sessions, FakeEmbedder(default=[1.0, 0.0]))
    await embedded(facts, "diogo", "allergic to shellfish")
    _, dup = await embedded(facts, "ana", "allergic to shellfish")
    assert dup is None
    assert len(await facts.list("diogo")) == len(await facts.list("ana")) == 1


async def test_facts_from_another_model_not_compared(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    old_facts = store(sessions, FakeEmbedder(default=[1.0, 0.0], model="old-model"))
    await embedded(old_facts, "diogo", "allergic to shellfish")
    facts = store(sessions, FakeEmbedder(default=[1.0, 0.0]))
    _, dup = await embedded(facts, "diogo", "allergic to shellfish")
    assert dup is None
    assert len(await facts.list("diogo")) == 2


async def test_embedder_error_leaves_the_fact_unembedded(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = store(sessions, FakeEmbedder([EmbeddingUnreachable("down")]))
    fid, dup = await embedded(facts, "diogo", "my car is electric", "told")
    assert dup is None
    row = await fact(sessions, fid)
    assert row.embedding is None and row.removed_at is None
    assert row.embedding_attempts == 1
    assert row.embedding_next_at == NOW + timedelta(seconds=30)
    assert [f.text for f in await facts.list("diogo")] == ["my car is electric"]


async def test_next_to_embed(sessions: async_sessionmaker[AsyncSession]) -> None:
    facts = store(sessions, FakeEmbedder(default=[1.0, 0.0]))
    assert await facts.next_to_embed() is None
    assert await facts.seconds_until_embedding_due() is None
    a, _ = await facts.add("diogo", "a", "told")
    b, _ = await facts.add("diogo", "b", "told")
    assert await facts.next_to_embed() == a
    async with sessions.begin() as session:
        await session.execute(
            update(Fact).where(Fact.id == a).values(embedding_next_at=NOW + timedelta(seconds=60))
        )
    assert await facts.next_to_embed() == b
    await facts.embed(b)
    assert await facts.next_to_embed() is None
    assert await facts.seconds_until_embedding_due() == 60
    # A removed fact is never embedded.
    await facts.forget("diogo", [a])
    assert await facts.seconds_until_embedding_due() is None
    async with sessions() as session:
        assert (await session.scalar(select(Fact.embedding).where(Fact.id == a))) is None
