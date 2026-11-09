from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.models import Conversation, Entry
from alfred.memory.embedding import EmbeddingUnreachable
from alfred.memory.search import NO_MEANING_NOTE, ConversationSearch
from tests.helpers import FakeEmbedder, make_config

CHICKEN = [1.0, 0.0, 0.0]
CHICKEN_ISH = [0.9, 0.1, 0.0]
DISK = [0.0, 1.0, 0.0]
OTHER = [0.0, 0.0, 1.0]


def at(day: int, hour: int = 12, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=UTC)


async def seed(
    sessions: async_sessionmaker[AsyncSession],
    cid: int,
    title: str | None,
    summary: str | None,
    *,
    started: datetime,
    ended: datetime | None = None,
    embedding: list[float] | None = None,
    model: str = "nomic-embed-text:v1.5",
    entries: tuple[str, ...] = (),
) -> None:
    async with sessions.begin() as session:
        session.add(
            Conversation(
                id=cid,
                started_at=started,
                ended_at=ended if ended is not None else started + timedelta(hours=1),
                title=title,
                summary=summary,
                embedding=embedding,
                embedding_model=model if embedding is not None else None,
            )
        )
        await session.flush()
        for line in entries:
            session.add(Entry(conversation_id=cid, kind="owner", text=line))


async def seed_current(sessions: async_sessionmaker[AsyncSession], *entries: str) -> None:
    async with sessions.begin() as session:
        session.add(Conversation(id=999, started_at=at(9)))
        await session.flush()
        for line in entries:
            session.add(Entry(conversation_id=999, kind="owner", text=line))


def memory(
    sessions: async_sessionmaker[AsyncSession],
    embedder: FakeEmbedder,
    **memory_config: object,
) -> ConversationSearch:
    cfg = make_config(memory=memory_config)  # type: ignore[arg-type]
    return ConversationSearch(cfg, sessions, embedder)


def ids(result_text: str) -> list[int]:
    """Conversation ids in the order the result lists them."""
    import re

    return [int(m) for m in re.findall(r"^#(\d+)", result_text, re.M)]


# --- by topic (6.1) --------------------------------------------------------------


async def test_search_by_topic(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed(
        sessions,
        41,
        "Chicken thighs recipe",
        "A lemon-garlic recipe, saved as recipe 12.",
        started=at(3, 10),
        embedding=CHICKEN_ISH,
    )
    await seed(sessions, 40, "Disk full", "Moved downloads.", started=at(2), embedding=DISK)
    embedder = FakeEmbedder([CHICKEN])
    result = await memory(sessions, embedder).search("chicken recipe")
    assert result.ok
    assert embedder.queries == ["chicken recipe"]
    assert ids(result.text)[0] == 41
    block = result.text.split("\n\n")[1]
    assert block == (
        '#41 "Chicken thighs recipe" (best match)\n'
        "Tue 3 Nov 2026 to Tue 3 Nov 2026\n"
        "A lemon-garlic recipe, saved as recipe 12."
    )


async def test_match_by_meaning_only(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed(sessions, 41, "Chicken thighs recipe", "Lemon.", started=at(3), embedding=CHICKEN)
    await seed(sessions, 40, "Disk full", "Moved downloads.", started=at(2), embedding=DISK)
    # No word of the query appears anywhere; only the vectors match.
    result = await memory(sessions, FakeEmbedder([CHICKEN_ISH])).search("poultry dinner")
    assert ids(result.text)[0] == 41


async def test_keyword_only_in_an_entry(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed(
        sessions,
        41,
        "Steak dinner",
        "Cooked a steak.",
        started=at(3),
        embedding=OTHER,
        entries=("should I cook it sous vide?", "Yes, 54°C for two hours."),
    )
    await seed(sessions, 40, "Disk full", "Moved downloads.", started=at(2), embedding=DISK)
    result = await memory(sessions, FakeEmbedder([DISK])).search("sous vide")
    # The vectors prefer #40; the keyword match in an entry still wins overall.
    assert ids(result.text)[0] == 41


async def test_two_equal_matches_are_close(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed(sessions, 41, "Chicken thighs", "Lemon chicken.", started=at(3), embedding=CHICKEN)
    await seed(sessions, 42, "Chicken wings", "Spicy chicken.", started=at(5), embedding=CHICKEN)
    await seed(sessions, 40, "Disk full", "Moved downloads.", started=at(2), embedding=DISK)
    result = await memory(sessions, FakeEmbedder([CHICKEN])).search("chicken")
    assert "(best match)" in result.text and "(close match)" in result.text
    assert '#40 "Disk full" (weak match)' in result.text


async def test_clear_winner(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed(sessions, 41, "Chicken thighs", "Lemon chicken.", started=at(3), embedding=CHICKEN)
    await seed(sessions, 40, "Disk full", "Moved downloads.", started=at(2), embedding=DISK)
    result = await memory(sessions, FakeEmbedder([CHICKEN])).search("chicken")
    assert "(close match)" not in result.text


async def test_current_conversation_excluded(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed(sessions, 41, "Chicken thighs recipe", "Lemon.", started=at(3), embedding=CHICKEN)
    await seed_current(sessions, "more chicken please")
    result = await memory(sessions, FakeEmbedder([CHICKEN])).search("chicken")
    assert ids(result.text) == [41]


async def test_recalled_text_does_not_match(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed(sessions, 41, "Chicken thighs recipe", "Lemon.", started=at(3), embedding=CHICKEN)
    await seed(sessions, 42, "Disk full", "Moved downloads.", started=at(4), embedding=DISK)
    async with sessions.begin() as session:
        session.add(
            Entry(
                conversation_id=42,
                kind="recall",
                text="#41 ... Owner: chicken thighs tonight?",
                recalled_conversation_id=41,
            )
        )
    # Keyword-only, so the vectors can't decide: #42's recall of #41 must not count.
    embedder = FakeEmbedder([EmbeddingUnreachable("down")])
    result = await memory(sessions, embedder).search("thighs")
    assert ids(result.text) == [41]


async def test_limit(sessions: async_sessionmaker[AsyncSession]) -> None:
    for i in range(1, 8):
        await seed(sessions, i, f"Chicken {i}", "Chicken again.", started=at(i), embedding=CHICKEN)
    result = await memory(sessions, FakeEmbedder([CHICKEN]), search_results=5).search("chicken")
    assert len(ids(result.text)) == 5
    assert result.text.startswith("5 archived conversations, best match first:")


async def test_only_rows_of_the_configured_model(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    # A two-dimensional vector from another model must not be compared (or crash).
    await seed(
        sessions,
        41,
        "Old",
        "Embedded by another model.",
        started=at(3),
        embedding=[1.0, 0.0],
        model="old-model",
    )
    await seed(sessions, 42, "New", "Embedded now.", started=at(4), embedding=CHICKEN)
    result = await memory(sessions, FakeEmbedder([CHICKEN])).search("something")
    assert ids(result.text) == [42]


async def test_ragman_down_falls_back_to_keywords(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(sessions, 41, "Chicken thighs recipe", "Lemon.", started=at(3), embedding=CHICKEN)
    await seed(sessions, 40, "Disk full", "Moved downloads.", started=at(2), embedding=DISK)
    embedder = FakeEmbedder([EmbeddingUnreachable("connection refused")])
    result = await memory(sessions, embedder).search("chicken recipe")
    assert result.ok
    assert result.text.startswith(NO_MEANING_NOTE)
    assert ids(result.text) == [41]


async def test_no_match(sessions: async_sessionmaker[AsyncSession]) -> None:
    embedder = FakeEmbedder([CHICKEN])
    result = await memory(sessions, embedder).search("chicken")
    assert result.ok and result.text == "No archived conversations match."


async def test_conversation_without_summary_yet(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(sessions, 41, None, None, started=at(3), entries=("chicken tonight?",))
    result = await memory(sessions, FakeEmbedder([CHICKEN])).search("chicken")
    assert "#41 (no summary yet) (best match)" in result.text


# --- by date (6.2) ----------------------------------------------------------------


async def chicken_conversations(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed(sessions, 30, "Chicken curry", "Curry.", started=at(10, month=10), embedding=CHICKEN)
    await seed(sessions, 41, "Chicken thighs", "Lemon.", started=at(3), embedding=CHICKEN)
    await seed(sessions, 45, "Chicken wings", "Spicy.", started=at(5), embedding=CHICKEN)
    await seed(sessions, 46, "Disk full", "Downloads.", started=at(6), embedding=DISK)


async def test_topic_and_date(sessions: async_sessionmaker[AsyncSession]) -> None:
    await chicken_conversations(sessions)
    result = await memory(sessions, FakeEmbedder([CHICKEN])).search(
        "chicken recipe", after="2026-11-02", before="2026-11-08"
    )
    assert sorted(ids(result.text)) == [41, 45, 46]  # #46 only by its (weak) vector
    assert 30 not in ids(result.text)
    assert '#46 "Disk full" (weak match)' in result.text
    assert ids(result.text)[:2] in ([41, 45], [45, 41])  # the two chicken ones lead


async def test_spanning_the_range_edge(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed(sessions, 41, "Long chat", "Talked for days.", started=at(1), ended=at(3))
    result = await memory(sessions, FakeEmbedder()).search(after="2026-11-02")
    assert ids(result.text) == [41]
    result = await memory(sessions, FakeEmbedder()).search(before="2026-11-02")
    assert ids(result.text) == [41]
    result = await memory(sessions, FakeEmbedder()).search(after="2026-11-04")
    assert ids(result.text) == []


async def test_local_midnight_in_lisbon(sessions: async_sessionmaker[AsyncSession]) -> None:
    # 23:10-23:30 UTC on 15 July is 00:10-00:30 on 16 July in Lisbon (summer time).
    started = datetime(2026, 7, 15, 23, 10, tzinfo=UTC)
    await seed(
        sessions,
        41,
        "Late chat",
        "After midnight.",
        started=started,
        ended=started + timedelta(minutes=20),
    )
    mem = memory(sessions, FakeEmbedder())
    on_15th = await mem.search(after="2026-07-15", before="2026-07-15")
    on_16th = await mem.search(after="2026-07-16", before="2026-07-16")
    assert ids(on_15th.text) == []
    assert ids(on_16th.text) == [41]
    assert "Thu 16 Jul 2026 to Thu 16 Jul 2026" in on_16th.text


async def test_dates_shown_in_configured_timezone(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(sessions, 41, "Chat", "x", started=datetime(2026, 11, 2, 20, tzinfo=UTC))
    # 20:00 UTC on 2 Nov is 05:00 on 3 Nov in Tokyo.
    cfg = make_config(ai={"timezone": "Asia/Tokyo"})
    result = await ConversationSearch(cfg, sessions, FakeEmbedder()).search(after="2026-11-03")
    assert "Tue 3 Nov 2026" in result.text


async def test_date_only_newest_first(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed(sessions, 41, "Morning", "Coffee.", started=at(3, 8))
    await seed(sessions, 42, "Evening", "Dinner.", started=at(3, 19))
    await seed(sessions, 43, "Next day", "Lunch.", started=at(4, 12))
    embedder = FakeEmbedder()
    result = await memory(sessions, embedder).search(after="2026-11-03", before="2026-11-03")
    assert result.text.startswith("2 archived conversations in that range, newest first:")
    assert ids(result.text) == [42, 41]
    assert "match)" not in result.text
    assert embedder.queries == []  # no embedding needed


async def test_empty_search_is_an_error(sessions: async_sessionmaker[AsyncSession]) -> None:
    result = await memory(sessions, FakeEmbedder()).search()
    assert not result.ok
    assert "query" in result.text and "date" in result.text
    blank = await memory(sessions, FakeEmbedder()).search("  ", "", None)
    assert not blank.ok


@pytest.mark.parametrize(
    ("after", "before", "named"),
    [
        ("last week", None, "after"),
        (None, "2026-13-01", "before"),
        ("02/11/2026", None, "after"),
        (20261102, None, "after"),
    ],
)
async def test_unparseable_date_is_an_error(
    sessions: async_sessionmaker[AsyncSession], after: object, before: object, named: str
) -> None:
    result = await memory(sessions, FakeEmbedder()).search("chicken", after, before)
    assert not result.ok
    assert result.text.startswith(f"Error: {named} ")


async def test_after_later_than_before(sessions: async_sessionmaker[AsyncSession]) -> None:
    result = await memory(sessions, FakeEmbedder()).search(after="2026-11-08", before="2026-11-02")
    assert not result.ok and "later than" in result.text
