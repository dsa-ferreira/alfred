"""The memory service: summaries, search and recall of archived conversations.

``summarise`` runs inline when a conversation ends (memory design D3) and in
the background sweeper (D4). It never raises: on any failure it schedules a
retry on the conversation row and the sweeper picks it up later.

The sweeper is its own task, not a queue item, so a summary that keeps
failing never holds up owner messages. It finds archived conversations that
lack a summary, or an embedding from the configured model, oldest first, and
waits for each one's ``summary_next_at``. This also backfills conversations
from before the upgrade and re-embeds them after a model change.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from alfred.agent.model import ModelError, ModelReply, token_counts
from alfred.config import Config
from alfred.db.models import Conversation, Entry
from alfred.history import current_id
from alfred.mcp.registry import ToolOutcome
from alfred.memory.embedding import EmbeddingError
from alfred.memory.transcript import (
    SummaryParseError,
    TranscriptEntry,
    cut_middle,
    date_span,
    parse_summary,
    recall_label,
    render,
    summary_messages,
)
from alfred.queue import backoff_s

log = logging.getLogger(__name__)

EMPTY_TITLE = "Empty conversation"
EMPTY_SUMMARY = "Nothing was said in this conversation."

# Reciprocal rank fusion constant (memory design D7).
RRF_K = 60
# A result whose fused score is at least this share of the best one's is "close".
CLOSE_RATIO = 0.9
OMITTED = "[earlier part omitted]"
NO_MEANING_NOTE = (
    "Note: matching by meaning is unavailable right now (the embedding service didn't "
    "answer), so these are keyword matches only."
)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CONVERSATION_ID = re.compile(r"#?\s*(\d+)")

# Keyword candidates: conversations whose title and summary, or any of whose
# entries (recalls excepted: they repeat another conversation), match any of
# the query's words. {dates} is replaced by the optional date conditions.
_KEYWORD_SQL = """
WITH q AS (
    SELECT to_tsquery('english', replace(plainto_tsquery('english', :query)::text, '&', '|'))
        AS tsq
),
hits AS (
    SELECT e.conversation_id, max(ts_rank(to_tsvector('english', e.text), q.tsq)) AS rank
    FROM entries e CROSS JOIN q
    WHERE to_tsvector('english', e.text) @@ q.tsq AND e.kind <> 'recall'
    GROUP BY e.conversation_id
)
SELECT c.id
FROM conversations c CROSS JOIN q LEFT JOIN hits h ON h.conversation_id = c.id
WHERE c.ended_at IS NOT NULL
  AND (c.search_tsv @@ q.tsq OR h.conversation_id IS NOT NULL)
  {dates}
ORDER BY greatest(ts_rank(c.search_tsv, q.tsq), coalesce(h.rank, 0)) DESC, c.id DESC
LIMIT :limit
"""


@dataclass(frozen=True)
class _Found:
    id: int
    title: str | None
    summary: str | None
    started_at: datetime
    ended_at: datetime | None


class Model(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


class Embedder(Protocol):
    model: str

    async def embed_document(self, text: str) -> list[float]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class Memory:
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
        self._zone = ZoneInfo(config.ai.timezone)
        # Set when a retry was scheduled, so the sweeper recomputes its sleep.
        self.changed = asyncio.Event()
        # One summary at a time per conversation (the worker and the sweeper).
        # Kept for the process's life: removing one could race with a waiter.
        self._locks: dict[int, asyncio.Lock] = {}

    # --- summaries ---------------------------------------------------------------

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

    # --- the sweeper -------------------------------------------------------------

    async def run_sweeper(self) -> None:
        log.info("memory sweeper started")
        last: int | None = None
        while True:
            self.changed.clear()  # before reading, so a nudge is never missed
            due = await self._next_due()
            if due is not None and due != last:
                last = due
                await self.summarise(due)
                continue
            if due is not None:
                # Still due right after it was processed: its retry couldn't be
                # recorded. Don't spin; try again after the shortest delay.
                wait = self._config.queue.retry_initial_s
            else:
                wait = await self._seconds_until_due()
            last = None
            try:
                async with asyncio.timeout(wait):
                    await self.changed.wait()
            except TimeoutError:
                pass

    async def sweep_once(self) -> float | None:
        """Process the oldest due conversation, or say how many seconds until one is due.

        Returns ``None`` after processing one.
        """
        due = await self._next_due()
        if due is None:
            return await self._seconds_until_due()
        await self.summarise(due)
        return None

    def _pending(self) -> Any:
        return (
            Conversation.ended_at.is_not(None),
            or_(
                Conversation.summary.is_(None),
                Conversation.embedding.is_(None),
                Conversation.embedding_model.is_distinct_from(self._embedder.model),
            ),
        )

    async def _next_due(self) -> int | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(Conversation.id)
                .where(
                    *self._pending(),
                    or_(
                        Conversation.summary_next_at.is_(None),
                        Conversation.summary_next_at <= self._now(),
                    ),
                )
                .order_by(Conversation.id)
                .limit(1)
            )

    async def _seconds_until_due(self) -> float:
        async with self._sessions() as session:
            soonest = await session.scalar(
                select(func.min(Conversation.summary_next_at)).where(*self._pending())
            )
        if soonest is None:
            return self._config.queue.retry_max_s
        return max(0.0, (soonest - self._now()).total_seconds())

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
            return await _transcript_entries(session, conversation_id)

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

    # --- search ------------------------------------------------------------------

    async def search(self, query: Any = None, after: Any = None, before: Any = None) -> ToolOutcome:
        """Archived conversations by topic, by date, or both (memory design D7)."""
        if query is not None and not isinstance(query, str):
            return ToolOutcome(False, "Error: query must be text.")
        query = (query or "").strip() or None
        try:
            lower = self._day_start(after, "after")
            upper = self._day_start(before, "before", next_day=True)
        except ValueError as exc:
            return ToolOutcome(False, f"Error: {exc}")
        if lower is not None and upper is not None and lower >= upper:
            return ToolOutcome(False, f"Error: after ({after}) is later than before ({before}).")
        if query is None and lower is None and upper is None:
            return ToolOutcome(
                False,
                "Error: give a query, a date range (after and/or before, YYYY-MM-DD), or both.",
            )

        dates: list[ColumnElement[bool]] = []
        if lower is not None:
            dates.append(Conversation.ended_at >= lower)  # it ended on or after the range starts
        if upper is not None:
            dates.append(Conversation.started_at < upper)  # it started before the range ends
        limit = self._memory.search_results

        if query is None:
            async with self._sessions() as session:
                rows = await session.execute(
                    select(*_FOUND_COLUMNS)
                    .where(Conversation.ended_at.is_not(None), *dates)
                    .order_by(Conversation.ended_at.desc(), Conversation.id.desc())
                    .limit(limit)
                )
                found = [_Found(*r) for r in rows]
            if not found:
                return ToolOutcome(True, "No archived conversations in that range.")
            head = f"{_count(len(found))} in that range, newest first:"
            return ToolOutcome(True, "\n\n".join([head, *(self._format(f) for f in found)]))

        note = None
        vector_ids: list[int] = []
        try:
            vector = await self._embedder.embed_query(query)
        except EmbeddingError as exc:
            log.warning("search %r: no meaning-based matches: %s", query, exc)
            note = NO_MEANING_NOTE
        else:
            vector_ids = await self._vector_candidates(vector, dates, limit * 2)
        keyword_ids = await self._keyword_candidates(query, lower, upper, limit * 2)

        scores: dict[int, float] = {}
        for ranking in (vector_ids, keyword_ids):
            for rank, cid in enumerate(ranking, start=1):
                scores[cid] = scores.get(cid, 0.0) + 1 / (RRF_K + rank)
        best = sorted(scores, key=lambda cid: (-scores[cid], -cid))[:limit]
        found_by_id = await self._load(best)
        lines = [note] if note else []
        if not best:
            lines.append("No archived conversations match.")
            return ToolOutcome(True, "\n\n".join(lines))
        lines.append(f"{_count(len(best))}, best match first:")
        top = scores[best[0]]
        for i, cid in enumerate(best):
            if i == 0:
                label = "best match"
            elif scores[cid] >= CLOSE_RATIO * top:
                label = "close match"
            else:
                label = "weak match"
            lines.append(self._format(found_by_id[cid], label))
        return ToolOutcome(True, "\n\n".join(lines))

    async def _vector_candidates(
        self, vector: list[float], dates: Sequence[ColumnElement[bool]], limit: int
    ) -> list[int]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(Conversation.id)
                .where(
                    Conversation.ended_at.is_not(None),
                    Conversation.embedding.is_not(None),
                    Conversation.embedding_model == self._embedder.model,
                    *dates,
                )
                .order_by(Conversation.embedding.cosine_distance(vector), Conversation.id.desc())
                .limit(limit)
            )
            return list(rows)

    async def _keyword_candidates(
        self, query: str, lower: datetime | None, upper: datetime | None, limit: int
    ) -> list[int]:
        conditions = []
        params: dict[str, Any] = {"query": query, "limit": limit}
        if lower is not None:
            conditions.append("AND c.ended_at >= :lower")
            params["lower"] = lower
        if upper is not None:
            conditions.append("AND c.started_at < :upper")
            params["upper"] = upper
        statement = text(_KEYWORD_SQL.format(dates=" ".join(conditions)))
        async with self._sessions() as session:
            return list(await session.scalars(statement, params))

    async def _load(self, ids: Sequence[int]) -> dict[int, _Found]:
        if not ids:
            return {}
        async with self._sessions() as session:
            rows = await session.execute(select(*_FOUND_COLUMNS).where(Conversation.id.in_(ids)))
            return {r.id: _Found(*r) for r in rows}

    def _day_start(self, value: Any, name: str, *, next_day: bool = False) -> datetime | None:
        """Local midnight starting ``value`` (or the day after), in the owner's timezone."""
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not _DATE.fullmatch(value.strip()):
            raise ValueError(f"{name} must be a date as YYYY-MM-DD, for example 2026-11-02.")
        try:
            day = date.fromisoformat(value.strip())
        except ValueError:
            raise ValueError(f"{name} is not a real date: {value.strip()}.") from None
        if next_day:
            day += timedelta(days=1)
        return datetime.combine(day, time(0), tzinfo=self._zone)

    def _format(self, found: _Found, label: str | None = None) -> str:
        title = f'#{found.id} "{found.title}"' if found.title else f"#{found.id} (no summary yet)"
        if label:
            title += f" ({label})"
        summary = found.summary or "(no summary yet)"
        return f"{title}\n{date_span(found.started_at, found.ended_at, self._zone)}\n{summary}"

    # --- recall ------------------------------------------------------------------

    async def recall(self, conversation_id: Any) -> ToolOutcome:
        """Bring an archived conversation into the current one (memory design D8)."""
        cid = _parse_id(conversation_id)
        if cid is None:
            return ToolOutcome(False, "Error: id must be a conversation number, such as 41.")
        async with self._sessions.begin() as session:
            # The same lock as History.append: a reset can't slip in between.
            current = await current_id(session, lock=True)
            if cid == current:
                return ToolOutcome(
                    False,
                    f"Error: #{cid} is the current conversation; it is already in your context.",
                )
            target = await session.get(Conversation, cid)
            if target is None:
                return ToolOutcome(False, f"Error: there is no conversation #{cid}.")
            if await self._already_recalled(session, current, cid):
                return ToolOutcome(False, _already(cid))
            entries = await _transcript_entries(session, cid)
            recalled_text = self._recall_text(target, entries)
            inserted = await session.execute(
                insert(Entry)
                .values(
                    conversation_id=current,
                    kind="recall",
                    text=recalled_text,
                    recalled_conversation_id=cid,
                )
                .on_conflict_do_nothing(
                    index_elements=["conversation_id", "recalled_conversation_id"],
                    index_where=text("kind = 'recall'"),
                )
            )
            if inserted.rowcount == 0:  # type: ignore[attr-defined]
                return ToolOutcome(False, _already(cid))
        log.info("conversation %s recalled into %s", cid, current)
        return ToolOutcome(True, recalled_text)

    async def _already_recalled(self, session: AsyncSession, current: int, cid: int) -> bool:
        found = await session.scalar(
            select(
                exists().where(
                    and_(
                        Entry.conversation_id == current,
                        Entry.kind == "recall",
                        Entry.recalled_conversation_id == cid,
                    )
                )
            )
        )
        return bool(found)

    def _recall_text(self, target: Conversation, entries: list[TranscriptEntry]) -> str:
        """Header and transcript, at most ``recall_max_chars``.

        Too long: the summary, an omission marker, then the end of the
        transcript that fits, starting at a line boundary where possible.
        """
        cap = self._memory.recall_max_chars
        header = (
            f"{recall_label(target.id, target.title)}, "
            f"{date_span(target.started_at, target.ended_at, self._zone)}"
        )
        body = render(entries)
        full = f"{header}\n{body}"
        if len(full) <= cap:
            return full
        prefix = f"{header}\nSummary: {target.summary or '(no summary yet)'}\n{OMITTED}\n"
        if len(prefix) >= cap:  # a tiny cap: the summary doesn't fit either
            prefix = f"{header}\n{OMITTED}\n"
        room = cap - len(prefix)
        if room <= 0:
            return full[-cap:]
        tail = body[-room:]
        newline = tail.find("\n")
        if 0 <= newline < len(tail) - 1:
            tail = tail[newline + 1 :]
        return prefix + tail


_FOUND_COLUMNS = (
    Conversation.id,
    Conversation.title,
    Conversation.summary,
    Conversation.started_at,
    Conversation.ended_at,
)


def _count(n: int) -> str:
    return f"{n} archived conversation" + ("" if n == 1 else "s")


def _already(cid: int) -> str:
    return (
        f"Error: #{cid} is already in this conversation (recalled earlier); use what it says there."
    )


def _parse_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str):
        match = _CONVERSATION_ID.fullmatch(value.strip())
        if match:
            return int(match.group(1)) or None
    return None


async def _transcript_entries(session: AsyncSession, conversation_id: int) -> list[TranscriptEntry]:
    recalled = aliased(Conversation)
    rows = await session.execute(
        select(
            Entry.kind,
            Entry.source,
            Entry.text,
            Entry.recalled_conversation_id,
            recalled.title,
        )
        .outerjoin(recalled, recalled.id == Entry.recalled_conversation_id)
        .where(Entry.conversation_id == conversation_id)
        .order_by(Entry.id)
    )
    return [TranscriptEntry(*row) for row in rows]
