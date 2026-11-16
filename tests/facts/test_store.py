"""The fact store: per-user facts, soft removal and near-duplicates (change add-user-facts)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.models import Fact
from alfred.facts.store import _DUPLICATE_CANDIDATES, FactStore, _DuplicateCheck, _FactsNotFound
from alfred.llm.embedding import _EmbeddingUnreachable
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
    with pytest.raises(_FactsNotFound) as exc:
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


# --- Embedding and duplicate candidates (change fix-fact-removals, design D2) -------------


async def embedded(facts: FactStore, user: str, fact_text: str, origin: str = "learned") -> int:
    fid, _ = await facts.add(user, fact_text, origin)  # type: ignore[arg-type]
    assert await facts.embed(fid)
    return fid


def candidate_ids(check: _DuplicateCheck | None) -> list[int]:
    assert check is not None
    return [f.id for f in check.candidates]


async def test_embedding_removes_nothing(sessions: async_sessionmaker[AsyncSession]) -> None:
    facts = store(sessions, FakeEmbedder(default=[1.0, 0.0]))
    a = await embedded(facts, "diogo", "allergic to shellfish")
    b = await embedded(facts, "diogo", "the user is allergic to shellfish")
    assert [f.id for f in await facts.list("diogo")] == [a, b]
    row = await fact(sessions, b)
    assert row.embedding_model == "nomic-embed-text:v1.5" and row.duplicate_checked_at is None


async def test_candidates_nearest_first_within_the_distance(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    vectors = [[1.0, 0.0], [0.0, 1.0], [0.9, 0.2], [0.99, 0.05], [1.0, 0.0]]
    facts = store(sessions, FakeEmbedder(vectors), facts_duplicate_max_distance=0.2)
    far = await embedded(facts, "diogo", "runs a homelab")  # [1, 0]
    await embedded(facts, "diogo", "likes jazz")  # [0, 1]: far away
    close = await embedded(facts, "diogo", "lives in Lisbon")  # [0.9, 0.2]
    closer = await embedded(facts, "diogo", "lives in Lisbon, Portugal")  # [0.99, 0.05]
    new = await embedded(facts, "diogo", "runs a home lab")  # [1, 0]
    check = await facts.duplicate_candidates(new)
    assert check is not None and check.fact.id == new and check.user_name == "diogo"
    assert candidate_ids(check) == [far, closer, close]


async def test_candidates_capped(sessions: async_sessionmaker[AsyncSession]) -> None:
    facts = store(sessions, FakeEmbedder(default=[1.0, 0.0]))
    ids = [await embedded(facts, "diogo", f"fact {i}") for i in range(8)]
    assert candidate_ids(await facts.duplicate_candidates(ids[-1])) == ids[:_DUPLICATE_CANDIDATES]


async def test_candidates_leave_out_removed_foreign_and_other_model_facts(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    old_model = store(sessions, FakeEmbedder(default=[1.0, 0.0], model="old-model"))
    await embedded(old_model, "diogo", "embedded with another model")
    facts = store(sessions, FakeEmbedder(default=[1.0, 0.0]))
    removed = await embedded(facts, "diogo", "forgotten")
    await facts.forget("diogo", [removed])
    await embedded(facts, "ana", "allergic to shellfish")
    kept = await embedded(facts, "diogo", "allergic to shellfish")
    new = await embedded(facts, "diogo", "is allergic to shellfish")
    assert candidate_ids(await facts.duplicate_candidates(new)) == [kept]


async def test_no_candidates_for_an_unembedded_or_removed_fact(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = store(sessions, FakeEmbedder(default=[1.0, 0.0]))
    plain, _ = await facts.add("diogo", "not embedded", "told")
    assert await facts.duplicate_candidates(plain) is None
    gone = await embedded(facts, "diogo", "gone")
    await facts.forget("diogo", [gone])
    assert await facts.duplicate_candidates(gone) is None


async def test_embedder_error_leaves_the_fact_unembedded(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    facts = store(sessions, FakeEmbedder([_EmbeddingUnreachable("down")]))
    fid, _ = await facts.add("diogo", "my car is electric", "told")
    assert not await facts.embed(fid)
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
