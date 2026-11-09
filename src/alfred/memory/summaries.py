"""Summaries of archived conversations: title, summary and embedding.

``summarise`` runs inline when a conversation ends (memory design D3) and in
the background sweeper (D4, ``alfred.memory.sweeper``). It never raises: on
any failure it schedules a retry on the conversation row and the sweeper
picks it up later.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from alfred.agent.model import ModelError, ModelReply, token_counts
from alfred.attachments.handles import refs_by_entry
from alfred.backoff import backoff_s
from alfred.config import Config
from alfred.db.models import Conversation, Entry
from alfred.memory.embedding import EmbeddingError
from alfred.memory.transcript import (
    SummaryParseError,
    TranscriptEntry,
    cut_middle,
    parse_summary,
    render,
    summary_messages,
)

log = logging.getLogger(__name__)

EMPTY_TITLE = "Empty conversation"
EMPTY_SUMMARY = "Nothing was said in this conversation."


class Model(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


class Embedder(Protocol):
    model: str

    async def embed_document(self, text: str) -> list[float]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class Summaries:
    def __init__(
        self,
        config: Config,
        sessions: async_sessionmaker[AsyncSession],
        model: Model,
        embedder: Embedder,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._memory = config.memory
        self._sessions = sessions
        self._model = model
        self._embedder = embedder
        self._now = now
        # Set when a retry was scheduled, so the sweeper recomputes its sleep.
        self.changed = asyncio.Event()
        # One summary at a time per conversation (the worker and the sweeper).
        # Kept for the process's life: removing one could race with a waiter.
        self._locks: dict[int, asyncio.Lock] = {}

    @property
    def embedding_model(self) -> str:
        """Conversations embedded with any other model are embedded again."""
        return self._embedder.model

    async def summarise(self, conversation_id: int) -> str | None:
        """Title, summarise and embed an archived conversation, as far as needed.

        Returns the title, or ``None`` if there is none. Never raises.
        """
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        try:
            async with lock:
                return await self._summarise(conversation_id)
        except Exception:
            log.exception("conversation %s: summarising failed unexpectedly", conversation_id)
            await self._retry_later(conversation_id, "internal error")
            return None
        finally:
            self.changed.set()  # the sweeper rechecks what is due

    async def _summarise(self, conversation_id: int) -> str | None:
        async with self._sessions() as session:
            row = await session.get(Conversation, conversation_id)
        if row is None or row.ended_at is None:
            return None  # only archived conversations are summarised
        title, summary = row.title, row.summary

        if summary is None:
            made = await self._make_summary(conversation_id)
            if made is None:
                return None
            title, summary = made
            async with self._sessions.begin() as session:
                await session.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(title=title, summary=summary, summarised_at=self._now())
                )
            log.info("conversation %s summarised: %r", conversation_id, title)

        if row.embedding is None or row.embedding_model != self._embedder.model:
            try:
                vector = await self._embedder.embed_document(f"{title}. {summary}")
            except EmbeddingError as exc:
                log.warning("conversation %s: embedding failed: %s", conversation_id, exc)
                await self._retry_later(conversation_id, str(exc))
                return title
            async with self._sessions.begin() as session:
                await session.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(embedding=vector, embedding_model=self._embedder.model)
                )

        async with self._sessions.begin() as session:
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(summary_attempts=0, summary_next_at=None)
            )
        return title

    async def _make_summary(self, conversation_id: int) -> tuple[str, str] | None:
        """One model request. ``None`` (and a retry scheduled) on failure."""
        entries = await self.transcript_entries(conversation_id)
        if not entries:
            return EMPTY_TITLE, EMPTY_SUMMARY
        transcript = cut_middle(render(entries), self._memory.summary_input_max_chars)
        try:
            reply = await self._model.complete(summary_messages(transcript), [])
            log.info("conversation %s summary: %s", conversation_id, token_counts(reply.usage))
            parsed = parse_summary(reply.content or "")
        except (ModelError, SummaryParseError) as exc:
            log.warning("conversation %s: summary failed: %s", conversation_id, exc)
            await self._retry_later(conversation_id, str(exc))
            return None
        return parsed.title, parsed.summary

    async def transcript_entries(self, conversation_id: int) -> list[TranscriptEntry]:
        async with self._sessions() as session:
            return await load_transcript(session, conversation_id)

    async def _retry_later(self, conversation_id: int, reason: str) -> None:
        try:
            async with self._sessions.begin() as session:
                attempts = await session.scalar(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(summary_attempts=Conversation.summary_attempts + 1)
                    .returning(Conversation.summary_attempts)
                )
                if attempts is None:
                    return
                delay = backoff_s(attempts, self._config.queue)
                await session.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(summary_next_at=self._now() + timedelta(seconds=delay))
                )
            log.info(
                "conversation %s: summary retry %d in %gs (%s)",
                conversation_id,
                attempts,
                delay,
                reason,
            )
        except Exception:
            log.exception("conversation %s: could not schedule a summary retry", conversation_id)
        finally:
            self.changed.set()


async def load_transcript(session: AsyncSession, conversation_id: int) -> list[TranscriptEntry]:
    recalled = aliased(Conversation)
    rows = await session.execute(
        select(
            Entry.kind,
            Entry.source,
            Entry.text,
            Entry.recalled_conversation_id,
            recalled.title,
            Entry.reminder_id,
            Entry.id,
        )
        .outerjoin(recalled, recalled.id == Entry.recalled_conversation_id)
        .where(Entry.conversation_id == conversation_id)
        .order_by(Entry.id)
    )
    entries = [TranscriptEntry(*row) for row in rows]
    refs = await refs_by_entry(session, (e.entry_id for e in entries if e.kind == "owner"))
    return [
        dataclasses.replace(e, attachments=refs[e.entry_id]) if e.entry_id in refs else e
        for e in entries
    ]
