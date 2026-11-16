from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.config import load_config
from alfred.facts.store import _FORGET, _REMEMBER, FactStore, FactTools
from alfred.kernel.access import AllowList, Gated
from alfred.kernel.toolset import AttemptContext, ToolOutcome
from alfred.kernel.turn import AskTurn, ChatTurn, ReminderTurn, TurnInput
from alfred.llm.embedding import _EmbeddingUnreachable
from tests.helpers import ANA, DIOGO, FakeEmbedder, make_config

OWNER = ChatTurn("remember the chicken?", 1, user=DIOGO)

ANA_CHAT = ChatTurn("hi", 2, user=ANA)


def fact_tools(
    sessions: async_sessionmaker[AsyncSession],
    embedder: FakeEmbedder | None = None,
    **memory: object,
) -> FactTools:
    cfg = make_config(memory=memory)  # type: ignore[arg-type]
    store = FactStore(cfg, sessions, embedder or FakeEmbedder(default=[1.0, 0.0]))
    return FactTools(store, cfg.memory)


async def remember(tools: FactTools, args: dict[str, Any], turn: TurnInput = OWNER) -> ToolOutcome:
    return await tools.call(_REMEMBER, args, AttemptContext(turn))


async def test_remember_stores_a_told_fact(sessions: async_sessionmaker[AsyncSession]) -> None:
    tools = fact_tools(sessions)
    outcome = await remember(tools, {"text": "  is vegetarian  "})
    assert outcome == ToolOutcome(True, "Saved as fact 1.")
    [fact] = await tools.facts.list("diogo")
    assert (fact.id, fact.text, fact.origin) == (1, "is vegetarian", "told")
    assert await tools.facts.list("ana") == []


async def test_remember_replaces(sessions: async_sessionmaker[AsyncSession]) -> None:
    tools = fact_tools(sessions, FakeEmbedder([[1.0, 0.0], [0.0, 1.0]]))
    await remember(tools, {"text": "lives in Lisbon"})
    outcome = await remember(tools, {"text": "lives in Porto", "replaces": [1]})
    assert outcome == ToolOutcome(True, "Saved as fact 2. Replaced 1.")
    assert [f.text for f in await tools.facts.list("diogo")] == ["lives in Porto"]


async def test_remember_with_foreign_or_unknown_replaces(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    tools = fact_tools(sessions, FakeEmbedder([[1.0, 0.0], [0.0, 1.0]]))
    await remember(tools, {"text": "lives in Lisbon"})  # diogo's fact 1
    await remember(tools, {"text": "likes tea"}, ANA_CHAT)  # ana's fact 2
    foreign = await remember(tools, {"text": "lives in Faro", "replaces": [1]}, ANA_CHAT)
    unknown = await remember(tools, {"text": "lives in Faro", "replaces": [99]}, ANA_CHAT)
    assert not foreign.ok and not unknown.ok
    assert foreign.text == unknown.text.replace("99", "1")
    assert "not found" in foreign.text
    assert [f.text for f in await tools.facts.list("ana")] == ["likes tea"]
    assert [f.text for f in await tools.facts.list("diogo")] == ["lives in Lisbon"]


@pytest.mark.parametrize("value", ["", "   ", None, 12, "x" * 21])
async def test_remember_refuses_empty_or_long_text(
    sessions: async_sessionmaker[AsyncSession], value: Any
) -> None:
    tools = fact_tools(sessions, fact_max_chars=20)
    outcome = await remember(tools, {"text": value})
    assert not outcome.ok and outcome.text.startswith("Error:")
    assert await tools.facts.list("diogo") == []


async def test_remember_a_duplicate(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Saved and embedded; the duplicate check runs later, in the background."""
    from alfred.db.models import Fact

    tools = fact_tools(sessions, FakeEmbedder(default=[1.0, 0.0]))
    await remember(tools, {"text": "allergic to shellfish"})
    outcome = await remember(tools, {"text": "is allergic to shellfish"})
    assert outcome == ToolOutcome(True, "Saved as fact 2.")
    assert [f.id for f in await tools.facts.list("diogo")] == [1, 2]
    async with sessions() as session:
        row = await session.get(Fact, 2)
    assert row is not None and row.embedding is not None and row.duplicate_checked_at is None


async def test_remember_works_without_the_embedder(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    tools = fact_tools(sessions, FakeEmbedder([_EmbeddingUnreachable("down")]))
    outcome = await remember(tools, {"text": "my car is electric"})
    assert outcome == ToolOutcome(True, "Saved as fact 1.")
    assert [f.text for f in await tools.facts.list("diogo")] == ["my car is electric"]


async def test_forget(sessions: async_sessionmaker[AsyncSession]) -> None:
    tools = fact_tools(sessions, FakeEmbedder([[1.0, 0.0], [0.0, 1.0], [0.6, 0.8]]))
    await remember(tools, {"text": "allergic to shellfish"})
    await remember(tools, {"text": "likes jazz"})
    await remember(tools, {"text": "likes tea"}, ANA_CHAT)  # ana's fact 3
    outcome = await tools.call(_FORGET, {"ids": [1, 3, 99]}, AttemptContext(OWNER))
    assert outcome == ToolOutcome(True, "Forgot 1. Not found: 3, 99.")
    assert [f.text for f in await tools.facts.list("diogo")] == ["likes jazz"]
    assert [f.text for f in await tools.facts.list("ana")] == ["likes tea"]
    # Foreign and unknown ids are reported in the same words.
    foreign = await tools.call(_FORGET, {"ids": [3]}, AttemptContext(OWNER))
    unknown = await tools.call(_FORGET, {"ids": [98]}, AttemptContext(OWNER))
    assert foreign == ToolOutcome(False, "Not found: 3.")
    assert unknown == ToolOutcome(False, "Not found: 98.")


async def test_fact_tools_offered_on_chat_turns_only(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    on = fact_tools(sessions, facts_in_context=True)
    off = fact_tools(sessions)
    for tools in (on, off):
        assert await tools.open(AskTurn("x", 2, "svc", user=DIOGO)) is None
        assert await tools.open(ReminderTurn("x", 3, user=DIOGO)) is None
    opened_on = await on.open(OWNER)
    opened_off = await off.open(OWNER)
    assert opened_on is not None and opened_off is not None
    assert [d["function"]["name"] for d in opened_on.definitions] == [_REMEMBER, _FORGET]
    assert [d["function"]["name"] for d in opened_off.definitions] == [_REMEMBER]
    # Facts in the context: a note says what the ids are for.
    assert len(opened_on.prompt_notes) == 1 and _FORGET in opened_on.prompt_notes[0].text
    assert opened_off.prompt_notes == ()


async def test_ids_note_names_only_allowed_tools(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    tools = fact_tools(sessions, facts_in_context=True)
    user = DIOGO.__class__("x", 1, "sir", tools=AllowList.of(_REMEMBER))
    opened = await tools.open(ChatTurn("hi", 1, user=user))
    assert opened is not None
    gated = Gated(opened, user.tools)
    assert [d["function"]["name"] for d in gated.definitions] == [_REMEMBER]
    assert _FORGET not in gated.prompt_notes[0].text and _REMEMBER in gated.prompt_notes[0].text


def test_allow_list_accepts_exact_fact_tool_names(tmp_path: Path) -> None:
    path = tmp_path / "alfred.toml"
    path.write_text(
        '[[users]]\nname = "diogo"\ntelegram_user_id = 42\nadmin = true\n'
        f'tools = ["{_REMEMBER}", "{_FORGET}"]\n\n[ai]\nmodel = "m"\n'
    )
    config = load_config(
        {
            "ALFRED_CONFIG": str(path),
            "ALFRED_TELEGRAM_BOT_TOKEN": "1:t",
            "ALFRED_DATABASE_URL": "postgresql://u:p@h/d",
        }
    )
    assert config.users.admin.tools.matches(_REMEMBER)
