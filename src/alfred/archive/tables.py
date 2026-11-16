"""The archive's table.

- ``conversation_summaries``: an archived conversation's title, summary and
  an embedding of both; ``search_tsv`` is generated from title and summary.
  ``attempts`` and ``next_at`` are the sweeper's retries. An archived
  conversation with no row, or one whose ``next_at`` has passed or whose
  embedding model is stale, is pending (``archive.summaries``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Computed, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from alfred.conversations.tables import Conversation
from alfred.db.base import Base

# What the full-text column is generated from.
_SEARCH_TSV = "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(summary, ''))"


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"
    __table_args__ = (
        Index("ix_conversation_summaries_search_tsv", "search_tsv", postgresql_using="gin"),
    )

    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(Conversation.id), primary_key=True
    )
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    summarised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # No fixed dimension: each row records the model that made it.
    embedding: Mapped[Any] = mapped_column(Vector(), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text)
    # Background retries of the summary or embedding (the sweeper).
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed(_SEARCH_TSV, persisted=True))
