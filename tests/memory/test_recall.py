from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.db.models import Entry
from alfred.history import History
from alfred.memory.search import OMITTED, ConversationSearch
from tests.helpers import FakeEmbedder, make_config
from tests.memory.test_search import at, seed

TRANSCRIPT = (
    '#41 "Chicken thighs recipe", Tue 3 Nov 2026 to Tue 3 Nov 2026\n'
    "Owner: chicken thighs tonight?\n"
    "Owner: lemon and garlic, 200°C"
)


def memory(
    sessions: async_sessionmaker[AsyncSession], **memory_config: object
) -> ConversationSearch:
    cfg = make_config(memory=memory_config)  # type: ignore[arg-type]
    return ConversationSearch(cfg, sessions, FakeEmbedder())


async def seed_41(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed(
        sessions,
        41,
        "Chicken thighs recipe",
        "Lemon-garlic thighs, 40 minutes at 200°C.",
        started=at(3, 10),
        entries=("chicken thighs tonight?", "lemon and garlic, 200°C"),
    )


async def recall_entries(sessions: async_sessionmaker[AsyncSession]) -> list[Entry]:
    async with sessions() as session:
        return list(await session.scalars(select(Entry).where(Entry.kind == "recall")))


async def test_recall_by_id(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed_41(sessions)
    await History(sessions).append("owner", "remember conversation 41")
    current = await History(sessions).current_conversation_id()
    result = await memory(sessions).recall(41)
    assert result.ok
    assert result.text == TRANSCRIPT
    (entry,) = await recall_entries(sessions)
    assert entry.conversation_id == current
    assert entry.recalled_conversation_id == 41
    assert entry.text == TRANSCRIPT
    assert entry.queue_item_id is None


async def test_id_given_as_text(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed_41(sessions)
    assert (await memory(sessions).recall("#41")).ok


async def test_unknown_id(sessions: async_sessionmaker[AsyncSession]) -> None:
    await History(sessions).append("owner", "remember conversation 9999")
    result = await memory(sessions).recall(9999)
    assert not result.ok
    assert result.text == "Error: there is no conversation #9999."
    assert await recall_entries(sessions) == []


async def test_bad_id(sessions: async_sessionmaker[AsyncSession]) -> None:
    for value in (None, "chicken", 0, -3, True, [41]):
        result = await memory(sessions).recall(value)
        assert not result.ok and "conversation number" in result.text


async def test_current_conversation_refused(sessions: async_sessionmaker[AsyncSession]) -> None:
    await History(sessions).append("owner", "hi")
    current = await History(sessions).current_conversation_id()
    result = await memory(sessions).recall(current)
    assert not result.ok and "current conversation" in result.text
    assert await recall_entries(sessions) == []


async def test_recalled_twice(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed_41(sessions)
    mem = memory(sessions)
    assert (await mem.recall(41)).ok
    again = await mem.recall(41)
    assert not again.ok and "already in this conversation" in again.text
    assert len(await recall_entries(sessions)) == 1


async def test_recall_again_after_reset(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed_41(sessions)
    mem = memory(sessions)
    assert (await mem.recall(41)).ok
    await History(sessions).reset()
    assert (await mem.recall(41)).ok  # a new conversation may recall it again


async def test_long_conversation_is_capped(sessions: async_sessionmaker[AsyncSession]) -> None:
    lines = tuple(f"line {i:04d} " + "x" * 90 for i in range(500))  # ~50,000 characters
    await seed(
        sessions,
        41,
        "Long chat",
        "A long talk about everything.",
        started=at(3),
        entries=lines,
    )
    result = await memory(sessions, recall_max_chars=32000).recall(41)
    assert result.ok
    assert len(result.text) <= 32000
    head, tail = result.text.split(f"\n{OMITTED}\n")
    assert head == '#41 "Long chat", Tue 3 Nov 2026 to Tue 3 Nov 2026\n' + (
        "Summary: A long talk about everything."
    )
    assert tail.startswith("Owner: line ")  # starts at a line boundary
    assert tail.endswith("line 0499 " + "x" * 90)
    (entry,) = await recall_entries(sessions)
    assert entry.text == result.text


async def test_cap_from_config(sessions: async_sessionmaker[AsyncSession]) -> None:
    lines = tuple(f"step {i}: " + "y" * 40 for i in range(10))
    await seed(sessions, 41, "Steps", "Ten steps.", started=at(3), entries=lines)
    result = await memory(sessions, recall_max_chars=300).recall(41)
    assert len(result.text) <= 300
    assert "Summary: Ten steps." in result.text and OMITTED in result.text
    assert result.text.endswith("step 9: " + "y" * 40)


async def test_tiny_cap_keeps_the_end(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed_41(sessions)
    result = await memory(sessions, recall_max_chars=100).recall(41)
    assert len(result.text) <= 100
    # No room for the summary: the header, the marker and as much of the end as fits.
    assert result.text.startswith('#41 "Chicken thighs recipe", Tue 3 Nov 2026')
    assert OMITTED in result.text and result.text.endswith("garlic, 200°C")


async def test_recall_inside_recalled_conversation_shows_title_only(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_41(sessions)
    await seed(sessions, 50, "Disk full", "Moved downloads.", started=at(5))
    async with sessions.begin() as session:
        session.add(
            Entry(
                conversation_id=50, kind="recall", text="the whole #41", recalled_conversation_id=41
            )
        )
    result = await memory(sessions).recall(50)
    assert '[recalled #41 "Chicken thighs recipe"]' in result.text
    assert "the whole #41" not in result.text
