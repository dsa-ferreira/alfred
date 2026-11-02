"""Conversation history: one current conversation, its entries, and resets.

The current conversation is the one row of ``conversations`` with
``ended_at IS NULL``. Each write reads it inside its own transaction, so an
entry written around a reset lands in whichever conversation is current when
it commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from alfred.db.models import Conversation, Entry
from alfred.memory.transcript import recall_label

EntryKind = Literal["owner", "assistant", "notice", "ask_prompt", "ask_reply"]


@dataclass(frozen=True)
class HistoryEntry:
    kind: str
    source: str | None
    text: str
    # For recall entries: the recalled conversation, its title and its summary.
    recalled_id: int | None = None
    recalled_title: str | None = None
    recalled_summary: str | None = None


@dataclass(frozen=True)
class Reset:
    """The conversation just archived (``None`` if there was none) and the new one."""

    archived_id: int | None
    new_id: int


class History:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def current_conversation_id(self) -> int:
        async with self._sessions.begin() as session:
            return await current_id(session, lock=False)

    async def append(
        self,
        kind: EntryKind,
        text_: str,
        source: str | None = None,
        queue_item_id: int | None = None,
    ) -> int:
        """Add an entry to the current conversation. Returns the conversation ID.

        With ``queue_item_id``, an entry of the same kind for that item that
        already exists is kept and nothing is added (a retried turn).
        """
        async with self._sessions.begin() as session:
            conversation_id = await current_id(session, lock=True)
            await session.execute(
                insert(Entry)
                .values(
                    conversation_id=conversation_id,
                    kind=kind,
                    source=source,
                    text=text_,
                    queue_item_id=queue_item_id,
                )
                .on_conflict_do_nothing(index_elements=["queue_item_id", "kind"])
            )
            return conversation_id

    async def recent(self, limit: int) -> list[HistoryEntry]:
        """The newest ``limit`` entries of the current conversation, oldest first.

        A recall counts as one entry, however long its transcript.
        """
        recalled = aliased(Conversation)
        async with self._sessions() as session:
            newest = (
                select(
                    Entry.id,
                    Entry.kind,
                    Entry.source,
                    Entry.text,
                    Entry.recalled_conversation_id,
                )
                .join(Conversation, Conversation.id == Entry.conversation_id)
                .where(Conversation.ended_at.is_(None))
                .order_by(Entry.id.desc())
                .limit(limit)
                .subquery()
            )
            rows = await session.execute(
                select(
                    newest.c.kind,
                    newest.c.source,
                    newest.c.text,
                    newest.c.recalled_conversation_id,
                    recalled.title,
                    recalled.summary,
                )
                .outerjoin(recalled, recalled.id == newest.c.recalled_conversation_id)
                .order_by(newest.c.id)
            )
            return [HistoryEntry(*row) for row in rows]

    async def reset(self) -> Reset:
        """Archive the current conversation and start a new one."""
        async with self._sessions.begin() as session:
            archived_id = await session.scalar(
                update(Conversation)
                .where(Conversation.ended_at.is_(None))
                .values(ended_at=func.now())
                .returning(Conversation.id)
            )
            new = Conversation()
            session.add(new)
            await session.flush()
            return Reset(archived_id, new.id)


async def current_id(session: AsyncSession, *, lock: bool) -> int:
    query = select(Conversation.id).where(Conversation.ended_at.is_(None))
    if lock:
        # A reset's UPDATE waits for this transaction, so the entry can't land
        # in a conversation that was archived before it committed.
        query = query.with_for_update(read=True)
    found = await session.scalar(query)
    if found is not None:
        return found
    await session.execute(
        insert(Conversation)
        .values()
        .on_conflict_do_nothing(
            index_elements=[text("(ended_at IS NULL)")], index_where=text("ended_at IS NULL")
        )
    )
    found = await session.scalar(query)
    assert found is not None
    return found


def to_messages(entries: list[HistoryEntry]) -> list[dict[str, Any]]:
    """Map entries to chat-completions messages.

    Owner messages and ask prompts are ``user`` turns (ask prompts prefixed
    ``[ask:<service>]``), replies are ``assistant`` turns, and notices are
    ``user`` turns prefixed ``[notice:<service>]``. Recalls are ``user`` turns
    prefixed ``[recall:#<id> "<title>"]``: the newest with its transcript,
    older ones with only the recalled conversation's summary (memory design D9).
    """
    newest_recall = max((i for i, e in enumerate(entries) if e.kind == "recall"), default=None)
    messages: list[dict[str, Any]] = []
    for i, e in enumerate(entries):
        match e.kind:
            case "recall":
                label = f"[recall:{recall_label(e.recalled_id, e.recalled_title)}]"
                if i == newest_recall:
                    content = f"{label} {e.text}"
                else:
                    content = f"{label} Summary: {e.recalled_summary or '(no summary yet)'}"
                messages.append({"role": "user", "content": content})
            case "owner":
                messages.append({"role": "user", "content": e.text})
            case "ask_prompt":
                messages.append({"role": "user", "content": f"[ask:{e.source}] {e.text}"})
            case "notice":
                messages.append({"role": "user", "content": f"[notice:{e.source}] {e.text}"})
            case "assistant" | "ask_reply":
                messages.append({"role": "assistant", "content": e.text})
    return messages
