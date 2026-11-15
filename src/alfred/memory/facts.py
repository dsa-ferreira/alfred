"""Facts about each user (change add-user-facts).

A fact is ``told`` (the user asked alfred to remember it) or ``learned``
(extracted from an archived conversation, ``alfred.memory.extract``). Every
read and write names the user, and only active facts (``removed_at IS NULL``)
are ever read by id, so another user's fact and a removed one both read as
not found.

Writes take one advisory lock per user (design D7), inside their
transaction: extraction apply, dedupe, ``alfred_remember``, ``alfred_forget``
and ``/memory forget``. Removal is an UPDATE of an active row, never a
DELETE, so two removals of the same fact mark it once. Embedding and model
requests happen outside the lock.

Each fact is embedded and compared with the user's other facts (design D6):
:meth:`FactStore.embed` does both, for ``alfred_remember`` inline and for the
sweeper's ``FactEmbeddingJob``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.backoff import backoff_s
from alfred.config import Config, MemoryConfig
from alfred.db.models import Fact
from alfred.memory.embedding import EmbeddingError
from alfred.memory.summaries import Embedder
from alfred.tools.base import (
    AttemptContext,
    LocalToolset,
    OpenToolset,
    PromptNote,
    ToolOutcome,
    TurnInput,
    chat_only,
    parse_arguments,
)

log = logging.getLogger(__name__)

Origin = Literal["told", "learned"]

_ACTIVE = Fact.removed_at.is_(None)


@dataclass(frozen=True)
class FactRow:
    id: int
    text: str
    origin: str
    created_at: datetime


@dataclass(frozen=True)
class Duplicate:
    """Two facts found to say the same thing: ``removed`` is marked a duplicate of ``kept``."""

    kept: int
    removed: int


class FactsNotFound(Exception):
    """Ids that aren't the user's active facts (unknown, removed or another user's)."""

    def __init__(self, ids: list[int]) -> None:
        super().__init__(f"not found: {', '.join(map(str, ids))}")
        self.ids = ids


async def lock_user(session: AsyncSession, user_name: str) -> None:
    """Serialise writes to ``user_name``'s facts until the transaction ends."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"facts:{user_name}"},
    )


async def mark_removed(
    session: AsyncSession,
    user_name: str,
    ids: Iterable[int],
    reason: Literal["forgotten", "replaced", "duplicate"],
    now: datetime,
    *,
    replaced_by: int | None = None,
    origin: Origin | None = None,
) -> list[FactRow]:
    """Mark those of ``ids`` that are ``user_name``'s active facts removed; returns them.

    ``origin`` limits it to facts of that origin. Call it under
    :func:`lock_user`.
    """
    wanted = sorted(set(ids))
    if not wanted:
        return []
    conditions = [Fact.user_name == user_name, _ACTIVE, Fact.id.in_(wanted)]
    if origin is not None:
        conditions.append(Fact.origin == origin)
    rows = await session.execute(
        update(Fact)
        .where(*conditions)
        .values(removed_at=now, removed_reason=reason, replaced_by=replaced_by)
        .returning(Fact.id, Fact.text, Fact.origin, Fact.created_at)
    )
    return sorted((FactRow(*r) for r in rows), key=lambda f: f.id)


async def insert_fact(
    session: AsyncSession,
    user_name: str,
    fact_text: str,
    origin: Origin,
    *,
    conversation_id: int | None = None,
) -> int:
    fact_id = await session.scalar(
        insert(Fact)
        .values(user_name=user_name, text=fact_text, origin=origin, conversation_id=conversation_id)
        .returning(Fact.id)
    )
    assert fact_id is not None
    return fact_id


class FactStore:
    def __init__(
        self,
        config: Config,
        sessions: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._sessions = sessions
        self._embedder = embedder
        self._now = now
        # Set when a fact may need embedding (added, or its retry scheduled),
        # so the sweeper's embedding job recomputes what is due.
        self.changed = asyncio.Event()

    @property
    def embedding_model(self) -> str:
        """Facts embedded with any other model are embedded again."""
        return self._embedder.model

    async def list(self, user_name: str) -> list[FactRow]:
        """``user_name``'s active facts, in id order."""
        async with self._sessions() as session:
            rows = await session.execute(
                select(Fact.id, Fact.text, Fact.origin, Fact.created_at)
                .where(Fact.user_name == user_name, _ACTIVE)
                .order_by(Fact.id)
            )
            return [FactRow(*r) for r in rows]

    async def add(
        self,
        user_name: str,
        fact_text: str,
        origin: Origin,
        conversation_id: int | None = None,
        *,
        replaces: Iterable[int] = (),
    ) -> tuple[int, list[FactRow]]:
        """Store a fact for ``user_name``, replacing ``replaces``; returns its id and those.

        Every id in ``replaces`` must be one of the user's active facts, of
        either origin; otherwise nothing is stored and :class:`FactsNotFound`
        names the missing ones. Doesn't embed: see :meth:`embed`.
        """
        wanted = sorted(set(replaces))
        async with self._sessions.begin() as session:
            await lock_user(session, user_name)
            if wanted:
                found = set(
                    await session.scalars(
                        select(Fact.id).where(
                            Fact.user_name == user_name, _ACTIVE, Fact.id.in_(wanted)
                        )
                    )
                )
                missing = [i for i in wanted if i not in found]
                if missing:
                    raise FactsNotFound(missing)
            fact_id = await insert_fact(
                session, user_name, fact_text, origin, conversation_id=conversation_id
            )
            replaced = await mark_removed(
                session, user_name, wanted, "replaced", self._now(), replaced_by=fact_id
            )
        self.changed.set()
        return fact_id, replaced

    async def forget(self, user_name: str, ids: Iterable[int]) -> tuple[list[FactRow], list[int]]:
        """Remove those of ``ids`` that are ``user_name``'s active facts.

        Returns the facts removed and, in order, the ids not found (unknown,
        already removed, or another user's: the same to the caller).
        """
        wanted = sorted(set(ids))
        async with self._sessions.begin() as session:
            await lock_user(session, user_name)
            removed = await mark_removed(session, user_name, wanted, "forgotten", self._now())
        gone = {f.id for f in removed}
        return removed, [i for i in wanted if i not in gone]

    async def replace(self, user_name: str, new_id: int, ids: Iterable[int]) -> list[int]:
        """Mark those of ``ids`` that are ``user_name``'s active ``learned`` facts replaced
        by ``new_id``; returns their ids. Others are left as they are."""
        async with self._sessions.begin() as session:
            await lock_user(session, user_name)
            removed = await self.replace_in(session, user_name, new_id, ids)
        return removed

    async def replace_in(
        self, session: AsyncSession, user_name: str, new_id: int, ids: Iterable[int]
    ) -> list[int]:
        """:meth:`replace` inside the caller's transaction, which holds the user's lock."""
        removed = await mark_removed(
            session, user_name, ids, "replaced", self._now(), replaced_by=new_id, origin="learned"
        )
        return [f.id for f in removed]

    # --- Embedding and near-duplicates (design D6) ------------------------------------

    async def embed(self, fact_id: int) -> Duplicate | None:
        """Embed an active fact and remove a near-duplicate, if any. Never raises.

        On an embedding failure the fact keeps no embedding and a retry is
        scheduled on its row. Returns the duplicate pair found, if any.
        """
        try:
            return await self._embed(fact_id)
        except Exception:
            log.exception("fact %s: embedding failed unexpectedly", fact_id)
            await self._retry_later(fact_id, "internal error")
            return None

    async def _embed(self, fact_id: int) -> Duplicate | None:
        async with self._sessions() as session:
            found = (
                await session.execute(
                    select(Fact.text, Fact.user_name).where(Fact.id == fact_id, _ACTIVE)
                )
            ).first()
        if found is None:
            return None
        fact_text, user_name = found
        model = self._embedder.model
        try:
            vector = await self._embedder.embed_document(fact_text)
        except EmbeddingError as exc:
            log.warning("fact %s: embedding failed: %s", fact_id, exc)
            await self._retry_later(fact_id, str(exc))
            return None

        async with self._sessions.begin() as session:
            await lock_user(session, user_name)
            stored = await session.scalar(
                update(Fact)
                .where(Fact.id == fact_id, _ACTIVE)
                .values(
                    embedding=vector,
                    embedding_model=model,
                    embedding_attempts=0,
                    embedding_next_at=None,
                )
                .returning(Fact.origin)
            )
            if stored is None:
                return None  # removed while it was being embedded
            distance = Fact.embedding.cosine_distance(vector)
            nearest = (
                await session.execute(
                    select(Fact.id, Fact.origin, distance)
                    .where(
                        Fact.user_name == user_name,
                        _ACTIVE,
                        Fact.id != fact_id,
                        Fact.embedding.is_not(None),
                        Fact.embedding_model == model,
                    )
                    .order_by(distance, Fact.id)
                    .limit(1)
                )
            ).first()
            if nearest is None or nearest[2] > self._config.memory.facts_duplicate_max_distance:
                return None
            pair = _winner((fact_id, stored), (nearest[0], nearest[1]))
            await mark_removed(
                session, user_name, [pair.removed], "duplicate", self._now(), replaced_by=pair.kept
            )
        log.info("fact %s duplicate of %s, removed %s", fact_id, nearest[0], pair.removed)
        return pair

    async def next_to_embed(self) -> int | None:
        """The oldest active fact due for an embedding (none, or from another model)."""
        async with self._sessions() as session:
            return await session.scalar(
                select(Fact.id)
                .where(
                    *self._needs_embedding(),
                    or_(Fact.embedding_next_at.is_(None), Fact.embedding_next_at <= self._now()),
                )
                .order_by(Fact.id)
                .limit(1)
            )

    async def seconds_until_embedding_due(self) -> float | None:
        """Until the soonest embedding retry, or ``None`` when no fact needs one."""
        async with self._sessions() as session:
            soonest = await session.scalar(
                select(func.min(Fact.embedding_next_at)).where(*self._needs_embedding())
            )
        if soonest is None:
            return None
        return max(0.0, (soonest - self._now()).total_seconds())

    def _needs_embedding(self) -> tuple[object, ...]:
        return (
            _ACTIVE,
            or_(
                Fact.embedding.is_(None),
                Fact.embedding_model.is_distinct_from(self._embedder.model),
            ),
        )

    async def _retry_later(self, fact_id: int, reason: str) -> None:
        try:
            async with self._sessions.begin() as session:
                attempts = await session.scalar(
                    update(Fact)
                    .where(Fact.id == fact_id)
                    .values(embedding_attempts=Fact.embedding_attempts + 1)
                    .returning(Fact.embedding_attempts)
                )
                if attempts is None:
                    return
                delay = backoff_s(attempts, self._config.queue)
                await session.execute(
                    update(Fact)
                    .where(Fact.id == fact_id)
                    .values(embedding_next_at=self._now() + timedelta(seconds=delay))
                )
            log.info("fact %s: embedding retry %d in %gs (%s)", fact_id, attempts, delay, reason)
        except Exception:
            log.exception("fact %s: could not schedule an embedding retry", fact_id)
        finally:
            self.changed.set()


def _winner(new: tuple[int, str], old: tuple[int, str]) -> Duplicate:
    """Which of two duplicates stays: a ``told`` fact over a ``learned`` one, else the older."""
    (a, a_origin), (b, b_origin) = new, old
    kept = (a if a_origin == "told" else b) if a_origin != b_origin else min(a, b)
    return Duplicate(kept=kept, removed=b if kept == a else a)


class FactEmbeddingJob:
    """The sweeper job that embeds and dedupes facts (design D6).

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


# --- Tools (design D8) ------------------------------------------------------------------

REMEMBER = "alfred_remember"
FORGET = "alfred_forget"
NAMES = (REMEMBER, FORGET)

REMEMBER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": REMEMBER,
        "description": (
            "Remember a lasting fact about the user for future conversations: use it when "
            "the user asks you to remember something about themselves, or corrects something "
            "you know about them. Not for one-off tasks; use a reminder for something due "
            "at a time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The fact, one short sentence, e.g. 'is vegetarian'.",
                },
                "replaces": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Ids of the user's facts this one corrects or makes outdated.",
                },
            },
            "required": ["text"],
        },
    },
}

FORGET_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": FORGET,
        "description": "Forget facts about the user, by their ids, when the user asks you to.",
        "parameters": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "The fact ids, e.g. [12].",
                }
            },
            "required": ["ids"],
        },
    },
}


def facts_note(remember: bool, forget: bool) -> str | None:
    """How the ids in the facts block are used, naming only the tools offered."""
    uses = [
        *([f"`{FORGET}` to forget facts the user wants forgotten"] if forget else []),
        *([f"`replaces` of `{REMEMBER}` when a fact corrects one"] if remember else []),
    ]
    if not uses:
        return None
    return (
        "The numbers in square brackets in what you know about the user are fact ids: "
        f"use them in {' and '.join(uses)}."
    )


class FactTools(LocalToolset):
    """``alfred_remember`` and, while facts are in the context, ``alfred_forget``.

    Offered on chat turns only. Both act on the turn user's facts.
    """

    def __init__(self, facts: FactStore, memory: MemoryConfig) -> None:
        self._facts = facts
        self._memory = memory

    def offered(self, turn: TurnInput) -> bool:
        return chat_only(turn)

    async def open(self, turn: TurnInput) -> OpenToolset | None:
        if not self.offered(turn):
            return None
        return _OpenFactTools(self, turn, forget=self._memory.facts_in_context)

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        arguments = parse_arguments(raw_arguments)
        if isinstance(arguments, str):
            return ToolOutcome(False, arguments)
        user = attempt.turn.user.name
        if name == REMEMBER:
            return await self._remember(user, arguments.get("text"), arguments.get("replaces"))
        return await self._forget(user, arguments.get("ids"))

    async def _remember(self, user: str, value: Any, replaces: Any) -> ToolOutcome:
        if not isinstance(value, str) or not value.strip():
            return ToolOutcome(False, "Error: give the fact as non-empty `text`.")
        fact_text = value.strip()
        limit = self._memory.fact_max_chars
        if len(fact_text) > limit:
            return ToolOutcome(
                False,
                f"Error: a fact is at most {limit} characters and this one has "
                f"{len(fact_text)}. Say it shorter.",
            )
        ids = _parse_ids(replaces if replaces is not None else [])
        if ids is None:
            return ToolOutcome(False, "Error: `replaces` must be a list of fact ids.")
        try:
            fact_id, replaced = await self.facts.add(user, fact_text, "told", replaces=ids)
        except FactsNotFound as exc:
            return ToolOutcome(
                False,
                f"Error: fact ids not found: {_ids(exc.ids)}. Nothing was saved or replaced.",
            )
        duplicate = await self.facts.embed(fact_id)
        if duplicate is not None and duplicate.removed == fact_id:
            return ToolOutcome(True, f"Already known as fact {duplicate.kept}.")
        saved = f"Saved as fact {fact_id}."
        if replaced:
            saved += f" Replaced {_ids(f.id for f in replaced)}."
        return ToolOutcome(True, saved)

    async def _forget(self, user: str, value: Any) -> ToolOutcome:
        ids = _parse_ids(value)
        if not ids:
            return ToolOutcome(False, "Error: give the ids of the facts to forget, e.g. [12].")
        removed, missing = await self.facts.forget(user, ids)
        parts = []
        if removed:
            parts.append(f"Forgot {_ids(f.id for f in removed)}.")
        if missing:
            parts.append(f"Not found: {_ids(missing)}.")
        return ToolOutcome(bool(removed), " ".join(parts))

    @property
    def facts(self) -> FactStore:
        return self._facts


class _OpenFactTools(LocalToolset):
    def __init__(self, tools: FactTools, turn: TurnInput, *, forget: bool) -> None:
        self._tools = tools
        self.definitions = [REMEMBER_TOOL, *([FORGET_TOOL] if forget else [])]
        if forget:
            # Only facts in the context have ids the model can see.
            allow = turn.user.tools
            note = facts_note(allow.matches(REMEMBER), allow.matches(FORGET))
            self.prompt_notes = (PromptNote(note),) if note else ()

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        return await self._tools.call(name, raw_arguments, attempt)


def _parse_ids(value: Any) -> list[int] | None:
    if isinstance(value, int | str | float) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list):
        return None
    ids: list[int] = []
    for item in value:
        if isinstance(item, bool):
            return None
        if isinstance(item, float) and item.is_integer():
            item = int(item)
        if isinstance(item, str) and item.strip().lstrip("#").isdigit():
            item = int(item.strip().lstrip("#"))
        if not isinstance(item, int):
            return None
        ids.append(item)
    return ids


def _ids(ids: Iterable[int]) -> str:
    return ", ".join(str(i) for i in ids)
