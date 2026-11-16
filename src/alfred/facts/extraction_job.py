"""Facts learned from archived conversations.

``FactExtractionJob`` is a sweeper job. Every archived conversation starts
with its extraction pending: it has no ``fact_extractions`` row, or one
with no ``outcome``. Archived conversations are extracted oldest first. A user's
conversations are extracted in order: one waits while an older one of the
same user is pending, even on its backoff; other users' don't hold it up.

The request goes to the summary model, only while the queue is quiet, and is
cancelled when a queue item arrives (``preempt``): then nothing is recorded
and no attempt counted. A failed request or an unreadable reply logs a
WARNING with the attempt count and is retried with backoff; with ``[memory]
facts_extraction_max_attempts`` above 0 it is given up after that many.

Every fact the model returns is stored as ``learned``. ``REPLACE <id>
"<quote>"`` removes fact <id> only if it was shown to the model, the
quote matches its text, and it is still one of the user's active learned
facts; anything else is a plain addition, and the refusal is logged
without either text.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from alfred.backoff import RetryDelays, backoff_s
from alfred.conversations.tables import Conversation
from alfred.conversations.transcript import RecalledLookup, cut_middle, load_transcript, render
from alfred.facts.config import FactsConfig
from alfred.facts.extraction_prompt import (
    ExtractionParseError,
    NewFact,
    extraction_entries,
    extraction_messages,
    parse_facts,
    quote_matches,
)
from alfred.facts.store import FactStore, insert_fact, lock_user
from alfred.facts.tables import FactExtraction
from alfred.llm.chat import ModelError, token_counts
from alfred.llm.protocols import ChatModel
from alfred.llm.streaming import complete_unless_preempted

log = logging.getLogger(__name__)

# The extraction row of the conversation, outer-joined: none yet means pending.
_EXTRACTION_OF = FactExtraction.conversation_id == Conversation.id


class FactExtractionJob:
    uses_model = True

    def __init__(
        self,
        memory: FactsConfig,
        retry: RetryDelays,
        sessions: async_sessionmaker[AsyncSession],
        model: ChatModel,
        facts: FactStore,
        *,
        input_max_chars: int,
        recalled: RecalledLookup | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._memory = memory
        self._retry = retry
        # The longest transcript sent, as for a summary.
        self._input_max_chars = input_max_chars
        self._sessions = sessions
        self._model = model
        self._facts = facts
        # Recalled conversations' titles in transcripts (``archive``).
        self._recalled = recalled
        self._now = now
        # Set when a retry was scheduled or an extraction ended.
        self.changed = asyncio.Event()

    async def next_due(self, model_allowed: bool) -> int | None:
        """The oldest pending conversation that is due and has no older pending one."""
        if not model_allowed:
            return None
        async with self._sessions() as session:
            return await session.scalar(
                select(Conversation.id)
                .outerjoin(FactExtraction, _EXTRACTION_OF)
                .where(
                    *_pending(),
                    or_(FactExtraction.next_at.is_(None), FactExtraction.next_at <= self._now()),
                    _first_of_its_user(),
                )
                .order_by(Conversation.id)
                .limit(1)
            )

    async def seconds_until_due(self, model_allowed: bool) -> float | None:
        if not model_allowed:
            return None  # the sweeper wakes when the queue goes quiet
        async with self._sessions() as session:
            soonest = await session.scalar(
                select(func.min(FactExtraction.next_at))
                .select_from(Conversation)
                .outerjoin(FactExtraction, _EXTRACTION_OF)
                .where(*_pending(), _first_of_its_user())
            )
        if soonest is None:
            return None
        return max(0.0, (soonest - self._now()).total_seconds())

    async def run(self, key: int, preempt: asyncio.Event | None) -> None:
        """Extract one conversation's facts. Never raises."""
        try:
            await self._extract(key, preempt)
        except Exception as exc:
            log.exception("conversation %s: fact extraction failed unexpectedly", key)
            await self._failed(key, f"internal error ({type(exc).__name__})")
        finally:
            self.changed.set()

    async def _extract(self, conversation_id: int, preempt: asyncio.Event | None) -> None:
        async with self._sessions() as session:
            user = await session.scalar(
                select(Conversation.user_name)
                .outerjoin(FactExtraction, _EXTRACTION_OF)
                .where(Conversation.id == conversation_id, *_pending())
            )
            if user is None:
                return
            entries = extraction_entries(
                await load_transcript(session, conversation_id, self._recalled)
            )
        if not any(e.kind == "user" for e in entries):
            await self._done(conversation_id)
            log.info("conversation %s facts: no message from its user", conversation_id)
            return

        transcript = cut_middle(render(entries), self._input_max_chars)
        known = await self._facts.list(user)
        messages = extraction_messages(user, known, transcript)
        try:
            reply = await complete_unless_preempted(self._model, messages, preempt)
            if reply is None:
                log.info("conversation %s: fact extraction gave way to the queue", conversation_id)
                return
            found = parse_facts(reply.content or "")
        except (ModelError, ExtractionParseError) as exc:
            await self._failed(conversation_id, str(exc))
            return

        # Only a fact the model was shown can be replaced; never one stored just now.
        shown = {f.id: f.text for f in known}
        added = replaced = refused = 0
        async with self._sessions.begin() as session:
            await lock_user(session, user)
            still_pending = await session.scalar(
                select(Conversation.id)
                .outerjoin(FactExtraction, _EXTRACTION_OF)
                .where(Conversation.id == conversation_id, *_pending())
            )
            if still_pending is None:
                return
            for fact in found:
                fact_id = await insert_fact(
                    session,
                    user,
                    fact.text[: self._memory.fact_max_chars].strip(),
                    "learned",
                    conversation_id=conversation_id,
                )
                added += 1
                if fact.replaces is None:
                    continue
                why = self._refusal(user, fact, shown)
                if why is None:
                    gone = await self._facts.replace_in(session, user, fact_id, [fact.replaces])
                    why = None if gone else "not a learned fact of the user"
                if why is None:
                    replaced += 1
                else:
                    refused += 1
                    log.info(
                        "conversation %s: replacement of fact %s refused (%s); "
                        "stored as an addition",
                        conversation_id,
                        fact.replaces,
                        why,
                    )
            await session.execute(
                _upsert(conversation_id, outcome="done", attempts=0, next_at=None)
            )
        log.info(
            "conversation %s facts: %d added, %d replaced, %d replacement%s refused; %s",
            conversation_id,
            added,
            replaced,
            refused,
            "" if refused == 1 else "s",
            token_counts(reply.usage),
        )
        if added:
            self._facts.changed.set()  # embed them, then check them for duplicates

    @staticmethod
    def _refusal(user: str, fact: NewFact, shown: dict[int, str]) -> str | None:
        """Why a replacement can't be applied as asked, or ``None`` if it may be."""
        assert fact.replaces is not None
        if fact.replaces not in shown:
            return "not a fact the model was shown"
        if fact.quote is None:
            return "no quote of the fact"
        if not quote_matches(fact.quote, shown[fact.replaces], user):
            return "the quote doesn't match the fact"
        return None

    async def _done(self, conversation_id: int) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                _upsert(conversation_id, outcome="done", attempts=0, next_at=None)
            )

    async def _failed(self, conversation_id: int, reason: str) -> None:
        """Count a failed attempt: retry later, or give up after the configured maximum."""
        try:
            async with self._sessions.begin() as session:
                attempts = await session.scalar(
                    insert(FactExtraction)
                    .values(conversation_id=conversation_id, attempts=1)
                    .on_conflict_do_update(
                        index_elements=[FactExtraction.conversation_id],
                        set_={"attempts": FactExtraction.attempts + 1},
                    )
                    .returning(FactExtraction.attempts)
                )
                assert attempts is not None
                log.warning(
                    "conversation %s: fact extraction failed (attempt %d): %s",
                    conversation_id,
                    attempts,
                    reason,
                )
                limit = self._memory.facts_extraction_max_attempts
                if limit > 0 and attempts >= limit:
                    await session.execute(_upsert(conversation_id, outcome="gave_up", next_at=None))
                    log.warning(
                        "conversation %s: gave up fact extraction after %d attempts",
                        conversation_id,
                        attempts,
                    )
                    return
                delay = backoff_s(attempts, self._retry.retry_initial_s, self._retry.retry_max_s)
                await session.execute(
                    _upsert(conversation_id, next_at=self._now() + timedelta(seconds=delay))
                )
        except Exception:
            log.exception("conversation %s: could not record a failed extraction", conversation_id)
        finally:
            self.changed.set()


def _pending() -> tuple[object, ...]:
    """Archived and not yet extracted: with ``FactExtraction`` outer-joined
    (``_EXTRACTION_OF``), a conversation with no row reads ``outcome`` NULL."""
    return (Conversation.ended_at.is_not(None), FactExtraction.outcome.is_(None))


def _first_of_its_user() -> object:
    """No older conversation of the same user is pending."""
    older = aliased(Conversation)
    extraction = aliased(FactExtraction)
    return ~(
        select(older.id)
        .outerjoin(extraction, extraction.conversation_id == older.id)
        .where(
            older.user_name == Conversation.user_name,
            older.id < Conversation.id,
            older.ended_at.is_not(None),
            extraction.outcome.is_(None),
        )
        .exists()
    )


def _upsert(conversation_id: int, **values: Any) -> Any:
    """Insert the conversation's extraction row with ``values``, or set them on it."""
    return (
        insert(FactExtraction)
        .values(conversation_id=conversation_id, **values)
        .on_conflict_do_update(index_elements=[FactExtraction.conversation_id], set_=values)
    )
