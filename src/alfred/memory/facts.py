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

Each fact is embedded (:meth:`FactStore.embed`, by ``alfred_remember``
inline and by the sweeper's ``FactEmbeddingJob``) and then checked for
duplicates (change fix-fact-removals): embedding distance only picks the
user's closest other facts (:meth:`FactStore.duplicate_candidates`), and the
model decides which say the same thing (``alfred.memory.duplicates``).
Measured distances can't tell a duplicate from a related or contradicting
fact, so distance alone never removes one.
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

_Origin = Literal["told", "learned"]

_ACTIVE = Fact.removed_at.is_(None)


# The most facts one duplicate check compares a new fact with (design D2).
_DUPLICATE_CANDIDATES = 5


# --- Tools (design D8) ------------------------------------------------------------------

_REMEMBER = "alfred_remember"
_FORGET = "alfred_forget"
NAMES = (_REMEMBER, _FORGET)

_REMEMBER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _REMEMBER,
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

_FORGET_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _FORGET,
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


async def lock_user(session: AsyncSession, user_name: str) -> None:
    """Serialise writes to ``user_name``'s facts until the transaction ends."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"facts:{user_name}"},
    )


async def insert_fact(
    session: AsyncSession,
    user_name: str,
    fact_text: str,
    origin: _Origin,
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

    async def list(self, user_name: str) -> list[_FactRow]:
        """``user_name``'s active facts, in id order."""
        async with self._sessions() as session:
            rows = await session.execute(
                select(Fact.id, Fact.text, Fact.origin, Fact.created_at)
                .where(Fact.user_name == user_name, _ACTIVE)
                .order_by(Fact.id)
            )
            return [_FactRow(*r) for r in rows]

    async def add(
        self,
        user_name: str,
        fact_text: str,
        origin: _Origin,
        conversation_id: int | None = None,
        *,
        replaces: Iterable[int] = (),
    ) -> tuple[int, list[_FactRow]]:
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
                    raise _FactsNotFound(missing)
            fact_id = await insert_fact(
                session, user_name, fact_text, origin, conversation_id=conversation_id
            )
            replaced = await _mark_removed(
                session, user_name, wanted, "replaced", self._now(), replaced_by=fact_id
            )
        self.changed.set()
        return fact_id, replaced

    async def forget(self, user_name: str, ids: Iterable[int]) -> tuple[list[_FactRow], list[int]]:
        """Remove those of ``ids`` that are ``user_name``'s active facts.

        Returns the facts removed and, in order, the ids not found (unknown,
        already removed, or another user's: the same to the caller).
        """
        wanted = sorted(set(ids))
        async with self._sessions.begin() as session:
            await lock_user(session, user_name)
            removed = await _mark_removed(session, user_name, wanted, "forgotten", self._now())
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
        removed = await _mark_removed(
            session, user_name, ids, "replaced", self._now(), replaced_by=new_id, origin="learned"
        )
        return [f.id for f in removed]

    # --- Embedding (design D6 of add-user-facts) ----------------------------------------

    async def embed(self, fact_id: int) -> bool:
        """Embed an active fact; whether its vector was stored. Never raises.

        On an embedding failure the fact keeps no embedding and a retry is
        scheduled on its row. Comparing it is the duplicate check's job.
        """
        try:
            return await self._embed(fact_id)
        except Exception:
            log.exception("fact %s: embedding failed unexpectedly", fact_id)
            await self._retry_later(fact_id, "internal error")
            return False

    async def duplicate_candidates(self, fact_id: int) -> _DuplicateCheck | None:
        """The user's other active facts within the duplicate distance, nearest first.

        ``None`` unless the fact is active and embedded with the configured
        model. Only facts embedded with that model are compared.
        """
        model = self._embedder.model
        async with self._sessions() as session:
            found = (
                await session.execute(
                    select(Fact.id, Fact.text, Fact.origin, Fact.created_at, Fact.user_name).where(
                        Fact.id == fact_id, _ACTIVE, Fact.embedding_model == model
                    )
                )
            ).first()
            if found is None:
                return None
            fact = _FactRow(*found[:4])
            user_name = found[4]
            vector = await session.scalar(select(Fact.embedding).where(Fact.id == fact_id))
            if vector is None:
                return None
            distance = Fact.embedding.cosine_distance(vector)
            rows = await session.execute(
                select(Fact.id, Fact.text, Fact.origin, Fact.created_at)
                .where(
                    Fact.user_name == user_name,
                    _ACTIVE,
                    Fact.id != fact_id,
                    Fact.embedding.is_not(None),
                    Fact.embedding_model == model,
                    distance <= self._config.memory.facts_duplicate_max_distance,
                )
                .order_by(distance, Fact.id)
                .limit(_DUPLICATE_CANDIDATES)
            )
            return _DuplicateCheck(fact, user_name, [_FactRow(*r) for r in rows])

    async def next_duplicate_check(self) -> int | None:
        """The oldest fact whose duplicate check is due."""
        async with self._sessions() as session:
            return await session.scalar(
                select(Fact.id)
                .where(
                    *self._duplicate_pending(),
                    or_(Fact.duplicate_next_at.is_(None), Fact.duplicate_next_at <= self._now()),
                )
                .order_by(Fact.id)
                .limit(1)
            )

    async def seconds_until_duplicate_check(self) -> float | None:
        """Until the soonest duplicate-check retry, or ``None`` when none is scheduled."""
        async with self._sessions() as session:
            soonest = await session.scalar(
                select(func.min(Fact.duplicate_next_at)).where(*self._duplicate_pending())
            )
        if soonest is None:
            return None
        return max(0.0, (soonest - self._now()).total_seconds())

    async def finish_duplicate_check(
        self, check: _DuplicateCheck, same: Iterable[int]
    ) -> list[_Duplicate] | None:
        """Remove the duplicates the model named and mark the fact checked.

        Only candidates still active count. For each, a ``told`` fact stays
        over a ``learned`` one, otherwise the older; once the checked fact
        itself is removed, the rest are left alone. ``None`` when the fact
        was removed or checked meanwhile (nothing changed).
        """
        fact_id = check.fact.id
        allowed = {f.id for f in check.candidates}
        removed: list[_Duplicate] = []
        async with self._sessions.begin() as session:
            await lock_user(session, check.user_name)
            origin = await session.scalar(
                select(Fact.origin).where(
                    Fact.id == fact_id, _ACTIVE, Fact.duplicate_checked_at.is_(None)
                )
            )
            if origin is None:
                return None
            for other in [i for i in dict.fromkeys(same) if i in allowed]:
                other_origin = await session.scalar(
                    select(Fact.origin).where(
                        Fact.id == other, Fact.user_name == check.user_name, _ACTIVE
                    )
                )
                if other_origin is None:
                    continue
                pair = _winner((fact_id, origin), (other, other_origin))
                await _mark_removed(
                    session,
                    check.user_name,
                    [pair.removed],
                    "duplicate",
                    self._now(),
                    replaced_by=pair.kept,
                )
                removed.append(pair)
                log.info("fact %s duplicate of %s, removed %s", fact_id, other, pair.removed)
                if pair.removed == fact_id:
                    break
            await session.execute(
                update(Fact)
                .where(Fact.id == fact_id)
                .values(
                    duplicate_checked_at=self._now(), duplicate_attempts=0, duplicate_next_at=None
                )
            )
        return removed

    async def duplicate_check_failed(self, fact_id: int) -> tuple[int, float] | None:
        """Count a failed check and schedule a retry; the attempts and the delay."""
        async with self._sessions.begin() as session:
            attempts = await session.scalar(
                update(Fact)
                .where(Fact.id == fact_id)
                .values(duplicate_attempts=Fact.duplicate_attempts + 1)
                .returning(Fact.duplicate_attempts)
            )
            if attempts is None:
                return None
            delay = backoff_s(attempts, self._config.queue)
            await session.execute(
                update(Fact)
                .where(Fact.id == fact_id)
                .values(duplicate_next_at=self._now() + timedelta(seconds=delay))
            )
        return attempts, delay

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

    async def _embed(self, fact_id: int) -> bool:
        async with self._sessions() as session:
            found = (
                await session.execute(select(Fact.text).where(Fact.id == fact_id, _ACTIVE))
            ).first()
        if found is None:
            return False
        (fact_text,) = found
        model = self._embedder.model
        try:
            vector = await self._embedder.embed_document(fact_text)
        except EmbeddingError as exc:
            log.warning("fact %s: embedding failed: %s", fact_id, exc)
            await self._retry_later(fact_id, str(exc))
            return False

        async with self._sessions.begin() as session:
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
        # None: removed while it was being embedded.
        self.changed.set()  # its duplicate check may be due now
        return stored is not None

    # --- The duplicate check's state (change fix-fact-removals, design D1, D2) ----------

    def _duplicate_pending(self) -> tuple[object, ...]:
        """Active facts not yet checked, embedded with the configured model."""
        return (
            _ACTIVE,
            Fact.duplicate_checked_at.is_(None),
            Fact.embedding.is_not(None),
            Fact.embedding_model == self._embedder.model,
        )

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


class FactEmbeddingJob:
    """The sweeper job that embeds facts (design D6; checking them is ``FactDuplicateJob``).

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
        if name == _REMEMBER:
            return await self._remember(user, arguments.get("text"), arguments.get("replaces"))
        return await self._forget(user, arguments.get("ids"))

    @property
    def facts(self) -> FactStore:
        return self._facts

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
        except _FactsNotFound as exc:
            return ToolOutcome(
                False,
                f"Error: fact ids not found: {_ids(exc.ids)}. Nothing was saved or replaced.",
            )
        # Embedded now, so its duplicate check is ready once the queue is quiet.
        # The check itself needs a model request: never inside this turn.
        await self.facts.embed(fact_id)
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


@dataclass(frozen=True)
class _FactRow:
    id: int
    text: str
    origin: str
    created_at: datetime


@dataclass(frozen=True)
class _DuplicateCheck:
    """A fact pending its duplicate check, and the user's facts close enough to compare."""

    fact: _FactRow
    user_name: str
    # Nearest first.
    candidates: list[_FactRow]


@dataclass(frozen=True)
class _Duplicate:
    """Two facts found to say the same thing: ``removed`` is marked a duplicate of ``kept``."""

    kept: int
    removed: int


class _FactsNotFound(Exception):
    """Ids that aren't the user's active facts (unknown, removed or another user's)."""

    def __init__(self, ids: list[int]) -> None:
        super().__init__(f"not found: {', '.join(map(str, ids))}")
        self.ids = ids


async def _mark_removed(
    session: AsyncSession,
    user_name: str,
    ids: Iterable[int],
    reason: Literal["forgotten", "replaced", "duplicate"],
    now: datetime,
    *,
    replaced_by: int | None = None,
    origin: _Origin | None = None,
) -> list[_FactRow]:
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
    return sorted((_FactRow(*r) for r in rows), key=lambda f: f.id)


def _winner(new: tuple[int, str], old: tuple[int, str]) -> _Duplicate:
    """Which of two duplicates stays: a ``told`` fact over a ``learned`` one, else the older."""
    (a, a_origin), (b, b_origin) = new, old
    kept = (a if a_origin == "told" else b) if a_origin != b_origin else min(a, b)
    return _Duplicate(kept=kept, removed=b if kept == a else a)


def _facts_note(remember: bool, forget: bool) -> str | None:
    """How the ids in the facts block are used, naming only the tools offered."""
    uses = [
        *([f"`{_FORGET}` to forget facts the user wants forgotten"] if forget else []),
        *([f"`replaces` of `{_REMEMBER}` when a fact corrects one"] if remember else []),
    ]
    if not uses:
        return None
    return (
        "The numbers in square brackets in what you know about the user are fact ids: "
        f"use them in {' and '.join(uses)}."
    )


class _OpenFactTools(LocalToolset):
    def __init__(self, tools: FactTools, turn: TurnInput, *, forget: bool) -> None:
        self._tools = tools
        self.definitions = [_REMEMBER_TOOL, *([_FORGET_TOOL] if forget else [])]
        if forget:
            # Only facts in the context have ids the model can see.
            allow = turn.user.tools
            note = _facts_note(allow.matches(_REMEMBER), allow.matches(_FORGET))
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
