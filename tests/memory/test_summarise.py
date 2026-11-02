from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.model import ModelUnreachable
from alfred.db.models import Conversation
from alfred.history import History
from alfred.memory.embedding import EmbeddingUnreachable
from alfred.memory.service import Memory
from alfred.memory.transcript import SUMMARY_PROMPT
from tests.helpers import FakeEmbedder, FakeModel, make_config, text

NOW = datetime(2026, 11, 2, 14, 5, tzinfo=UTC)
REPLY = "Title: Chicken thighs recipe\nSummary: A lemon-garlic recipe, saved as recipe 12."


def memory(
    sessions: async_sessionmaker[AsyncSession],
    model: FakeModel,
    embedder: FakeEmbedder,
    **memory_config: object,
) -> Memory:
    cfg = make_config(
        queue={"retry_initial_s": 30, "retry_max_s": 900},
        memory=memory_config,  # type: ignore[arg-type]
    )
    return Memory(cfg, sessions, model, embedder, now=lambda: NOW)


async def archived(sessions: async_sessionmaker[AsyncSession], *lines: str) -> int:
    history = History(sessions)
    await history.current_conversation_id()  # exists even with no entries
    for i, line in enumerate(lines):
        await history.append("owner" if i % 2 == 0 else "assistant", line)
    reset = await history.reset()
    assert reset.archived_id is not None
    return reset.archived_id


async def row(sessions: async_sessionmaker[AsyncSession], conversation_id: int) -> Conversation:
    async with sessions() as session:
        found = await session.get(Conversation, conversation_id)
    assert found is not None
    return found


async def test_summary_and_embedding_stored(sessions: async_sessionmaker[AsyncSession]) -> None:
    cid = await archived(sessions, "chicken thighs tonight?", "Lemon and garlic, sir.")
    model = FakeModel([text(REPLY)])
    embedder = FakeEmbedder([[0.1, 0.2, 0.3]])
    title = await memory(sessions, model, embedder).summarise(cid)

    assert title == "Chicken thighs recipe"
    assert len(model.requests) == 1
    messages, tools = model.requests[0]
    assert tools == []
    assert messages[0] == {"role": "system", "content": SUMMARY_PROMPT}
    assert (
        messages[1]["content"] == "Owner: chicken thighs tonight?\nalfred: Lemon and garlic, sir."
    )
    assert embedder.documents == [
        "Chicken thighs recipe. A lemon-garlic recipe, saved as recipe 12."
    ]
    stored = await row(sessions, cid)
    assert stored.title == "Chicken thighs recipe"
    assert stored.summary == "A lemon-garlic recipe, saved as recipe 12."
    assert stored.summarised_at == NOW
    assert [round(x, 3) for x in stored.embedding] == [0.1, 0.2, 0.3]
    assert stored.embedding_model == "nomic-embed-text:v1.5"
    assert stored.summary_attempts == 0 and stored.summary_next_at is None


async def test_embedding_failure_keeps_title_and_summary(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    cid = await archived(sessions, "chicken?", "Lemon.")
    mem = memory(sessions, FakeModel([text(REPLY)]), FakeEmbedder([EmbeddingUnreachable("down")]))
    assert await mem.summarise(cid) == "Chicken thighs recipe"
    stored = await row(sessions, cid)
    assert stored.title == "Chicken thighs recipe" and stored.summary is not None
    assert stored.embedding is None and stored.embedding_model is None
    assert stored.summary_attempts == 1
    assert stored.summary_next_at == NOW + timedelta(seconds=30)
    assert mem.changed.is_set()


async def test_model_failure_schedules_retry(sessions: async_sessionmaker[AsyncSession]) -> None:
    cid = await archived(sessions, "chicken?", "Lemon.")
    embedder = FakeEmbedder()
    mem = memory(sessions, FakeModel([ModelUnreachable("refused")]), embedder)
    assert await mem.summarise(cid) is None
    stored = await row(sessions, cid)
    assert stored.title is None and stored.summary is None and stored.embedding is None
    assert stored.summary_attempts == 1
    assert stored.summary_next_at == NOW + timedelta(seconds=30)
    assert embedder.documents == []
    assert mem.changed.is_set()


async def test_backoff_grows_with_attempts(sessions: async_sessionmaker[AsyncSession]) -> None:
    cid = await archived(sessions, "chicken?", "Lemon.")
    mem = memory(sessions, FakeModel([ModelUnreachable("x")] * 3), FakeEmbedder())
    for _ in range(3):
        await mem.summarise(cid)
    stored = await row(sessions, cid)
    assert stored.summary_attempts == 3
    assert stored.summary_next_at == NOW + timedelta(seconds=120)


async def test_unusable_reply_schedules_retry(sessions: async_sessionmaker[AsyncSession]) -> None:
    cid = await archived(sessions, "chicken?", "Lemon.")
    mem = memory(sessions, FakeModel([text("I'd rather not.")]), FakeEmbedder())
    assert await mem.summarise(cid) is None
    assert (await row(sessions, cid)).summary_attempts == 1


async def test_success_after_failure_clears_retry(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    cid = await archived(sessions, "chicken?", "Lemon.")
    mem = memory(
        sessions, FakeModel([ModelUnreachable("x"), text(REPLY)]), FakeEmbedder(default=[1.0])
    )
    assert await mem.summarise(cid) is None
    assert await mem.summarise(cid) == "Chicken thighs recipe"
    stored = await row(sessions, cid)
    assert stored.summary_attempts == 0 and stored.summary_next_at is None


async def test_only_embedding_redone(sessions: async_sessionmaker[AsyncSession]) -> None:
    cid = await archived(sessions, "chicken?", "Lemon.")
    model = FakeModel([text(REPLY)])
    first = memory(sessions, model, FakeEmbedder([EmbeddingUnreachable("down")]))
    await first.summarise(cid)
    second = memory(sessions, model, FakeEmbedder([[0.5]]))
    assert await second.summarise(cid) == "Chicken thighs recipe"
    assert len(model.requests) == 1  # no second summary request
    assert (await row(sessions, cid)).embedding is not None


async def test_long_transcript_is_shortened(sessions: async_sessionmaker[AsyncSession]) -> None:
    cid = await archived(sessions, "start " + "x" * 3000, "y" * 3000 + " end")
    model = FakeModel([text(REPLY)])
    mem = memory(sessions, model, FakeEmbedder(default=[1.0]), summary_input_max_chars=1000)
    assert await mem.summarise(cid) == "Chicken thighs recipe"
    sent = model.requests[0][0][1]["content"]
    assert len(sent) <= 1000
    assert sent.startswith("Owner: start") and sent.endswith(" end")
    assert "characters omitted" in sent


async def test_current_conversation_is_not_summarised(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    cid = await History(sessions).append("owner", "still talking")
    model = FakeModel()
    assert await memory(sessions, model, FakeEmbedder()).summarise(cid) is None
    assert model.requests == []


async def test_empty_conversation_needs_no_model(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    cid = await archived(sessions)
    model = FakeModel()
    assert await memory(sessions, model, FakeEmbedder([[1.0]])).summarise(cid) == (
        "Empty conversation"
    )
    assert model.requests == []
