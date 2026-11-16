"""The background job that embeds facts."""

from __future__ import annotations

import asyncio

from alfred.features.facts.store import FactStore


class FactEmbeddingJob:
    """The sweeper job that embeds facts (checking them is ``FactDuplicateJob``).

    Facts with no embedding, or one from another model, oldest first, each
    after its own ``embedding_next_at``. It needs no chat model, so it never
    waits for a quiet queue, and it never gives up: the fact works without
    an embedding meanwhile.
    """

    uses_model = False

    def __init__(self, facts: FactStore) -> None:
        self._facts = facts

    @property
    def changed(self) -> asyncio.Event:
        return self._facts.changed

    async def next_due(self, model_allowed: bool) -> int | None:
        return await self._facts.next_to_embed()

    async def seconds_until_due(self, model_allowed: bool) -> float | None:
        return await self._facts.seconds_until_embedding_due()

    async def run(self, key: int, preempt: asyncio.Event | None) -> None:
        await self._facts.embed(key)
