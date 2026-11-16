"""Summaries of archived conversations: title, summary and embedding.

``summarise`` runs in the background job runner (``alfred.archive.summary_job``)
only; ending a conversation just wakes it. It never raises: on any failure it
schedules a retry on the conversation's ``conversation_summaries`` row and the
sweeper picks it up later.

:func:`recalled` is ``conversations``' ``RecalledLookup``: recall entries show
the titles and summaries kept here.

The summary request gives way to the queue: when ``preempt`` is set while
it runs, it is cancelled and nothing is recorded, so the attempt doesn't
count as a failure. The embedding request goes to another server and is
never cancelled.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.archive.config import ArchiveConfig
from alfred.archive.prompts import SummaryParseError, parse_summary, summary_messages
from alfred.archive.tables import ConversationSummary
from alfred.backoff import RetryDelays, backoff_s
from alfred.conversations.tables import Conversation
from alfred.conversations.transcript import (
    Recalled,
    TranscriptEntry,
    cut_middle,
    load_transcript,
    render,
)
from alfred.llm.chat import ModelError, token_counts
from alfred.llm.embedding import EmbeddingError
from alfred.llm.protocols import ChatModel, Embedder
from alfred.llm.streaming import complete_unless_preempted

log = logging.getLogger(__name__)

_EMPTY_TITLE = "Empty conversation"
_EMPTY_SUMMARY = "Nothing was said in this conversation."


class _Preempted:
    """The summary request was cancelled because a queue item arrived."""


_PREEMPTED = _Preempted()


class Summaries:
    def __init__(
        self,
        memory: ArchiveConfig,
        retry: RetryDelays,
        sessions: async_sessionmaker[AsyncSession],
        model: ChatModel,
        embedder: Embedder,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._memory = memory
        self._retry = retry
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

    async def summarise(
        self, conversation_id: int, preempt: asyncio.Event | None = None
    ) -> str | None:
        """Title, summarise and embed an archived conversation, as far as needed.

        Returns the title, or ``None`` if there is none. A summary request
        still running when ``preempt`` is set is cancelled, and this returns
        ``None`` with no retry scheduled. Never raises.
        """
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        try:
            async with lock:
                return await self._summarise(conversation_id, preempt)
        except Exception:
            log.exception("conversation %s: summarising failed unexpectedly", conversation_id)
            await self._retry_later(conversation_id, "internal error")
            return None
        finally:
            self.changed.set()  # the sweeper rechecks what is due

    async def _summarise(self, conversation_id: int, preempt: asyncio.Event | None) -> str | None:
        async with self._sessions() as session:
            ended_at = await session.scalar(
                select(Conversation.ended_at).where(Conversation.id == conversation_id)
            )
            row = await session.get(ConversationSummary, conversation_id)
        if ended_at is None:
            return None  # only archived conversations are summarised
        title = row.title if row is not None else None
        summary = row.summary if row is not None else None
        embedded = row is not None and row.embedding is not None
        embedding_model = row.embedding_model if row is not None else None

        if summary is None:
            made = await self._make_summary(conversation_id, preempt)
            if made is None or made is _PREEMPTED:
                return None
            title, summary = made
            await self._save(
                conversation_id, title=title, summary=summary, summarised_at=self._now()
            )
            log.info("conversation %s summarised: %r", conversation_id, title)

        if not embedded or embedding_model != self._embedder.model:
            try:
                vector = await self._embedder.embed_document(f"{title}. {summary}")
            except EmbeddingError as exc:
                log.warning("conversation %s: embedding failed: %s", conversation_id, exc)
                await self._retry_later(conversation_id, str(exc))
                return title
            await self._save(
                conversation_id, embedding=vector, embedding_model=self._embedder.model
            )

        await self._save(conversation_id, attempts=0, next_at=None)
        return title

    async def _make_summary(
        self, conversation_id: int, preempt: asyncio.Event | None
    ) -> tuple[str, str] | _Preempted | None:
        """One model request. ``None`` (and a retry scheduled) on failure;
        ``PREEMPTED`` (and nothing recorded) when ``preempt`` won."""
        entries = await self._transcript_entries(conversation_id)
        if not entries:
            return _EMPTY_TITLE, _EMPTY_SUMMARY
        transcript = cut_middle(render(entries), self._memory.summary_input_max_chars)
        try:
            reply = await complete_unless_preempted(
                self._model, summary_messages(transcript), preempt
            )
            if reply is None:
                log.info("conversation %s: summary request gave way to the queue", conversation_id)
                return _PREEMPTED
            log.info("conversation %s summary: %s", conversation_id, token_counts(reply.usage))
            parsed = parse_summary(reply.content or "")
        except (ModelError, SummaryParseError) as exc:
            log.warning("conversation %s: summary failed: %s", conversation_id, exc)
            await self._retry_later(conversation_id, str(exc))
            return None
        return parsed.title, parsed.summary

    async def _transcript_entries(self, conversation_id: int) -> list[TranscriptEntry]:
        async with self._sessions() as session:
            return await load_transcript(session, conversation_id, recalled)

    async def _retry_later(self, conversation_id: int, reason: str) -> None:
        try:
            async with self._sessions.begin() as session:
                attempts = await session.scalar(
                    insert(ConversationSummary)
                    .values(conversation_id=conversation_id, attempts=1)
                    .on_conflict_do_update(
                        index_elements=[ConversationSummary.conversation_id],
                        set_={"attempts": ConversationSummary.attempts + 1},
                    )
                    .returning(ConversationSummary.attempts)
                )
                assert attempts is not None
                delay = backoff_s(attempts, self._retry.retry_initial_s, self._retry.retry_max_s)
                await session.execute(
                    _upsert(conversation_id, next_at=self._now() + timedelta(seconds=delay))
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

    async def _save(self, conversation_id: int, **values: Any) -> None:
        """Set ``values`` on the conversation's summary row, creating it if needed."""
        async with self._sessions.begin() as session:
            await session.execute(_upsert(conversation_id, **values))


async def recalled(session: AsyncSession, conversation_ids: Collection[int]) -> dict[int, Recalled]:
    """The titles and summaries of ``conversation_ids`` that have them.

    ``conversations``' ``RecalledLookup``.
    """
    rows = await session.execute(
        select(
            ConversationSummary.conversation_id,
            ConversationSummary.title,
            ConversationSummary.summary,
        ).where(ConversationSummary.conversation_id.in_(list(conversation_ids)))
    )
    return {cid: Recalled(title, summary) for cid, title, summary in rows}


def _upsert(conversation_id: int, **values: Any) -> Any:
    """Insert the conversation's summary row with ``values``, or set them on it."""
    return (
        insert(ConversationSummary)
        .values(conversation_id=conversation_id, **values)
        .on_conflict_do_update(index_elements=[ConversationSummary.conversation_id], set_=values)
    )
