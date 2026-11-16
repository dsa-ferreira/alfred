"""The duplicate check of each new fact.

``FactDuplicateJob`` is a sweeper job that runs after the embedding job. For
each active, embedded fact whose check is pending, oldest first, it takes up
to five of the user's facts within ``[memory] facts_duplicate_max_distance``
(:meth:`FactStore.duplicate_candidates`). With none, the fact is checked
with no model request. Otherwise the summary model says which of them say
the same thing, and only those are removed, with the usual winner rule.

Like extraction, the request starts only while the queue is quiet and gives
way to an arriving queue item (nothing recorded, no attempt counted). A
failed request or unreadable reply logs a WARNING with the attempt count and
is retried with backoff; it never gives up, since a pending check blocks
nothing and the fact is in use meanwhile.
"""

from __future__ import annotations

import asyncio
import logging

from alfred.facts.extraction_prompt import DuplicateParseError, duplicate_messages, parse_duplicates
from alfred.facts.store import FactStore
from alfred.llm.chat import ModelError, token_counts
from alfred.llm.protocols import ChatModel
from alfred.llm.streaming import complete_unless_preempted

log = logging.getLogger(__name__)


class FactDuplicateJob:
    uses_model = True

    def __init__(self, facts: FactStore, model: ChatModel) -> None:
        self._facts = facts
        self._model = model

    @property
    def changed(self) -> asyncio.Event:
        """The store's: set when a fact is added or embedded."""
        return self._facts.changed

    async def next_due(self, model_allowed: bool) -> int | None:
        # A fact with no candidate needs no model, but finding that out is the
        # same query as for one that does: wait for a quiet queue either way.
        if not model_allowed:
            return None
        return await self._facts.next_duplicate_check()

    async def seconds_until_due(self, model_allowed: bool) -> float | None:
        if not model_allowed:
            return None  # the sweeper wakes when the queue goes quiet
        return await self._facts.seconds_until_duplicate_check()

    async def run(self, key: int, preempt: asyncio.Event | None) -> None:
        """Check one fact. Never raises."""
        try:
            await self._check(key, preempt)
        except Exception as exc:
            log.exception("fact %s: duplicate check failed unexpectedly", key)
            await self._failed(key, f"internal error ({type(exc).__name__})")
        finally:
            self._facts.changed.set()

    async def _check(self, fact_id: int, preempt: asyncio.Event | None) -> None:
        check = await self._facts.duplicate_candidates(fact_id)
        if check is None:
            return  # removed, or re-embedded with another model meanwhile
        if not check.candidates:
            await self._facts.finish_duplicate_check(check, [])
            return

        messages = duplicate_messages(check.user_name, check.fact.text, check.candidates)
        try:
            reply = await complete_unless_preempted(self._model, messages, preempt)
            if reply is None:
                log.info("fact %s: duplicate check gave way to the queue", fact_id)
                return
            same = parse_duplicates(reply.content or "")
        except (ModelError, DuplicateParseError) as exc:
            await self._failed(fact_id, str(exc))
            return

        removed = await self._facts.finish_duplicate_check(check, same)
        if removed is None:
            return
        log.info(
            "fact %s duplicate check: %d compared, %d duplicate; %s",
            fact_id,
            len(check.candidates),
            len(removed),
            token_counts(reply.usage),
        )

    async def _failed(self, fact_id: int, reason: str) -> None:
        try:
            scheduled = await self._facts.duplicate_check_failed(fact_id)
        except Exception:
            log.exception("fact %s: could not record a failed duplicate check", fact_id)
            return
        if scheduled is not None:
            attempts, delay = scheduled
            log.warning(
                "fact %s: duplicate check failed (attempt %d), retry in %gs: %s",
                fact_id,
                attempts,
                delay,
                reason,
            )
