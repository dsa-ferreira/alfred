"""Search and recall of archived conversations (memory design D7, D8).

:class:`MemoryTools` offers them to the model on owner turns as
``alfred_search_conversations`` and ``alfred_recall_conversation``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, exists, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alfred.agent.toolset import (
    AttemptContext,
    LocalToolset,
    ToolOutcome,
    owner_only,
    parse_arguments,
)
from alfred.config import Config
from alfred.db.models import Conversation, Entry
from alfred.history import current_id
from alfred.memory.embedding import EmbeddingError
from alfred.memory.summaries import load_transcript
from alfred.memory.transcript import TranscriptEntry, date_span, recall_label, render

if TYPE_CHECKING:
    from alfred.agent.turn import TurnInput

log = logging.getLogger(__name__)

SEARCH_CONVERSATIONS = "alfred_search_conversations"
RECALL_CONVERSATION = "alfred_recall_conversation"

SEARCH_CONVERSATIONS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SEARCH_CONVERSATIONS,
        "description": (
            "Search earlier, archived conversations with the owner by topic, by date, or "
            "both. Use when the owner refers to something discussed before ('remember "
            "when…', 'that recipe we talked about last week', 'what did we talk about on "
            "Tuesday?'). Turn relative dates into after/before (YYYY-MM-DD, inclusive) using "
            "today's date from the system prompt. If one result is clearly the best match, "
            "recall it and tell the owner which conversation you picked (its title and "
            "date). If several match about equally, ask the owner which one they meant."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What the conversation was about, in a few words.",
                },
                "after": {
                    "type": "string",
                    "description": "Earliest date, YYYY-MM-DD, inclusive.",
                },
                "before": {
                    "type": "string",
                    "description": "Latest date, YYYY-MM-DD, inclusive.",
                },
            },
        },
    },
}

RECALL_CONVERSATION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": RECALL_CONVERSATION,
        "description": (
            "Bring an archived conversation, by its number, into this conversation so you "
            "can use what was said. Use when the owner names a conversation number or after "
            "search found the one they mean."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "The conversation number, e.g. 41."}
            },
            "required": ["id"],
        },
    },
}

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


class QueryEmbedder(Protocol):
    model: str

    async def embed_query(self, text: str) -> list[float]: ...


class ConversationSearch:
    def __init__(
        self,
        config: Config,
        sessions: async_sessionmaker[AsyncSession],
        embedder: QueryEmbedder,
    ) -> None:
        self._memory = config.memory
        self._sessions = sessions
        self._embedder = embedder
        self._zone = ZoneInfo(config.ai.timezone)

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
            entries = await load_transcript(session, cid)
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


class Searcher(Protocol):
    async def search(
        self, query: Any = None, after: Any = None, before: Any = None
    ) -> ToolOutcome: ...

    async def recall(self, conversation_id: Any) -> ToolOutcome: ...


class MemoryTools(LocalToolset):
    """Search and recall, offered on owner turns."""

    definitions = [SEARCH_CONVERSATIONS_TOOL, RECALL_CONVERSATION_TOOL]

    def __init__(self, search: Searcher) -> None:
        self._search = search

    def offered(self, turn: TurnInput) -> bool:
        return owner_only(turn)

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        arguments = parse_arguments(raw_arguments)
        if isinstance(arguments, str):
            return ToolOutcome(False, arguments)
        if name == SEARCH_CONVERSATIONS:
            return await self._search.search(
                arguments.get("query"), arguments.get("after"), arguments.get("before")
            )
        return await self._search.recall(arguments.get("id"))


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
