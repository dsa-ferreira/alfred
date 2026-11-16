"""The facts tables.

- ``facts``: short facts about a user, ``told`` by the user or ``learned``
  from an archived conversation. Removal is soft: ``removed_at``,
  ``removed_reason`` and, for replaced and duplicate facts,
  ``replaced_by``.
- ``fact_extractions``: learning facts from an archived conversation. One
  with no row, or a row with no ``outcome``, is pending; ``attempts`` and
  ``next_at`` are the retries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from alfred.features.conversations.tables import Conversation
from alfred.infra.db.base import Base, in_values

_FACT_ORIGINS = ("told", "learned")
_FACT_REMOVAL_REASONS = ("forgotten", "replaced", "duplicate")
_EXTRACTION_OUTCOMES = ("done", "gave_up")


class Fact(Base):
    __tablename__ = "facts"
    __table_args__ = (
        CheckConstraint(in_values("origin", _FACT_ORIGINS), name="origin"),
        CheckConstraint(in_values("removed_reason", _FACT_REMOVAL_REASONS), name="removed_reason"),
        CheckConstraint("(removed_at IS NULL) = (removed_reason IS NULL)", name="removed"),
        # Every read but an audit is of a user's active facts.
        Index(
            "ix_facts_active",
            "user_name",
            "id",
            postgresql_where=text("removed_at IS NULL"),
        ),
        # Active facts whose duplicate check is pending.
        Index(
            "ix_facts_duplicate_pending",
            "id",
            postgresql_where=text("removed_at IS NULL AND duplicate_checked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_name: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The conversation a learned fact came from.
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey(Conversation.id, ondelete="SET NULL")
    )
    embedding: Mapped[Any] = mapped_column(Vector(), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text)
    embedding_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    embedding_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_reason: Mapped[str | None] = mapped_column(Text)
    replaced_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("facts.id"))
    # The model's duplicate check: null while pending.
    duplicate_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duplicate_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duplicate_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FactExtraction(Base):
    __tablename__ = "fact_extractions"
    __table_args__ = (CheckConstraint(in_values("outcome", _EXTRACTION_OUTCOMES), name="outcome"),)

    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(Conversation.id), primary_key=True
    )
    # Null while pending.
    outcome: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
